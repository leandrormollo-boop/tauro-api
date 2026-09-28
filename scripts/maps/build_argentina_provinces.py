"""Build visual province boundaries from a locally downloaded Georef GeoJSON."""
import argparse
import json
import math
from pathlib import Path


def simplify(points, epsilon=.025):
    if len(points) <= 3:
        return points
    a, b = points[0], points[-1]
    dx, dy = b[0]-a[0], b[1]-a[1]
    denom = dx*dx + dy*dy
    def distance(p):
        t = max(0, min(1, ((p[0]-a[0])*dx+(p[1]-a[1])*dy)/denom)) if denom else 0
        return math.hypot(p[0]-a[0]-t*dx, p[1]-a[1]-t*dy)
    i = max(range(1, len(points)-1), key=lambda n: distance(points[n]))
    if distance(points[i]) > epsilon:
        return simplify(points[:i+1], epsilon)[:-1] + simplify(points[i:], epsilon)
    return [a, b]


def ring(points):
    # D3's spherical polygons use clockwise exterior rings.
    rounded = [[round(x, 3), round(y, 3)] for x, y, *_ in points]
    reduced = simplify(rounded)
    if len(reduced) < 4:
        reduced = rounded
    return reduced


def build(source):
    features = []
    provinces = {}
    for f in source['features']:
        p = f['properties']; code = p['iso_id'].removeprefix('AR-')
        geometry = f['geometry']
        polygons = geometry['coordinates'] if geometry['type'] == 'MultiPolygon' else [geometry['coordinates']]
        normalized = []
        for poly in polygons:
            rings = []
            for i, coords in enumerate(poly):
                pts = ring(coords)
                signed = sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(pts,pts[1:]))
                # Tiny islands can collapse to a line after rounding. A zero-area
                # spherical ring may otherwise be interpreted as the whole globe.
                if abs(signed) < .00001:
                    if i == 0: break
                    continue
                if (signed > 0) == (i == 0): pts.reverse()
                rings.append(pts)
            if rings: normalized.append(rings)
        features.append({'type':'Feature','id':code,'properties':{},'geometry':{'type':'MultiPolygon','coordinates':normalized}})
        center = [round(p['centroide']['lon'],4), round(p['centroide']['lat'],4)]
        # IGN's full jurisdiction centroid for Tierra del Fuego is in Antarctica.
        # Use the provincial capital as the visible reference for this shipping view.
        if code == 'V': center = [-68.3,-54.8]
        provinces[code] = {'name':p['iso_nombre'], 'center':center}
    return {'type':'FeatureCollection','features':features,'provinces':provinces}


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('source',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args()
    args.output.write_text(json.dumps(build(json.loads(args.source.read_text())),ensure_ascii=False,separators=(',',':'))+'\n')

# Local geographic renderer

`geo.js` is an ES module bundle built with esbuild 0.27.3 from:

- d3-geo 3.1.1 (ISC)
- d3-array 3.2.4 (ISC)
- internmap 2.0.3 (ISC)

The original license notices are included alongside the bundle.

To rebuild in a temporary directory, install those exact package versions and
esbuild 0.27.3. Create `geo-entry.js` containing:

```js
export {geoOrthographic,geoPath,geoGraticule10,geoInterpolate,geoDistance,geoArea} from 'd3-geo';
```

Then run:

```sh
npx esbuild geo-entry.js --bundle --minify --format=esm --outfile=geo.js
```

Copy the result and dependency license notices here. No Node dependencies or
build tools are required in production; all runtime assets are served locally.

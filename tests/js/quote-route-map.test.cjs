const test=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const path=require('node:path');
const {normalize,resolveCity,resolveLocation}=require('../../static/js/quote-route-map.js');
const data=(code)=>JSON.parse(fs.readFileSync(path.join(__dirname,'../../static/data/quote-map/cities',code+'.json')));
const world=JSON.parse(fs.readFileSync(path.join(__dirname,'../../static/data/quote-map/world.json')));
test('recognizes accents and source-language city aliases without changing country',()=>{
  assert.equal(normalize('  Shanghái '),'shanghai');
  assert.deepEqual(resolveCity('Shanghai',data('CN')).point,resolveCity('Shanghái',data('CN')).point);
  assert.ok(resolveCity('Nueva Delhi',data('IN')));
  assert.ok(resolveCity('Dhaka',data('BD')));
  assert.equal(resolveCity('Shanghai',data('AR')),null);
});
test('unknown or incomplete city uses an explicitly approximate country point',()=>{
  const result=resolveLocation('CN','Lugar inexistente',world.countries,data('CN'));
  assert.equal(result.precise,false);assert.equal(result.unresolved,true);assert.equal(result.city,'');
  assert.deepEqual(result.point,world.countries.CN.center);
  assert.equal(resolveCity('Shang',data('CN')),null);
  assert.equal(resolveLocation('ZZ','Shanghai',world.countries,data('CN')),null);
});
test('homonymous cities do not silently select the most populous',()=>{
  const rows=[{name:'San José',aliases:['San José'],point:[1,2]},{name:'San José',aliases:['San Jose'],point:[3,4]}];
  assert.equal(resolveCity('San Jose',rows),null);
});
test('country city catalogs are consistent and have valid coordinates',()=>{
  for(const [code,country]of Object.entries(world.countries)){
    assert.match(code,/^[A-Z]{2}$/);
    if(!country.cities)continue;
    for(const city of data(code)){
      assert.ok(city.point[0]>=-180&&city.point[0]<=180);assert.ok(city.point[1]>=-90&&city.point[1]<=90);
      assert.ok(city.name&&city.aliases.length);
    }
  }
});
test('curved-view camera keeps both cities visible, including long and reverse routes',async()=>{
  const {geoInterpolate,geoDistance}=await import('../../static/vendor/quote-map/geo.js');
  const {routeCenter}=require('../../static/js/quote-route-map.js');
  for(const [a,b]of [[[121.43,31.21],[-58.43,-34.61]],[[179,20],[-179,25]],[[0,0],[0,0]],[[0,0],[180,0]]]){
    const mid=geoInterpolate(a,b)(.5),center=routeCenter(a,b,mid);
    assert.ok(center.every(Number.isFinite));
    assert.ok(geoDistance(center,a)<=Math.PI/2+1e-6);assert.ok(geoDistance(center,b)<=Math.PI/2+1e-6);
    const reversed=routeCenter(b,a,geoInterpolate(b,a)(.5));
    if(geoDistance(a,b)<Math.PI-1e-6)assert.ok(geoDistance(center,reversed)<1e-6);
  }
});
test('all 24 Argentine jurisdictions match the form codes and stay within visible Argentina',async()=>{
  const provinces=JSON.parse(fs.readFileSync(path.join(__dirname,'../../static/data/quote-map/argentina.json')));
  const {resolveProvince}=require('../../static/js/quote-route-map.js');
  const {geoArea}=await import('../../static/vendor/quote-map/geo.js');
  assert.deepEqual(Object.keys(provinces.provinces).sort(),['C','B','K','H','U','X','W','E','P','Y','L','F','M','N','Q','R','A','J','D','Z','S','G','V','T'].sort());
  for(const feature of provinces.features){
    const p=resolveProvince(feature.id,provinces);
    assert.ok(p && p.city);assert.equal(p.country,'AR');assert.equal(p.precise,false);
    assert.ok(p.point[0]>=-75&&p.point[0]<=-50);assert.ok(p.point[1]>=-56&&p.point[1]<=-20);
    assert.ok(geoArea(feature)>0 && geoArea(feature)<2*Math.PI,'D3 winding: '+feature.id);
  }
  assert.equal(resolveProvince('ZZ',provinces),null);
});
test('drag wraps longitude, limits poles, and scales movement with zoom',()=>{
  const {dragCenter,mapZoom}=require('../../static/js/quote-route-map.js');
  const start=[175,80];
  const drag=dragCenter(start,-200,200,400,400,1);
  assert.deepEqual(start,[175,80]);
  assert.ok(drag[0]>=-180&&drag[0]<180);assert.equal(drag[1],85);
  assert.equal(dragCenter([0,0],0,-10000,400,400,1)[1],-85);
  assert.ok(Math.abs(dragCenter([0,0],100,0,400,400,4)[0])<Math.abs(dragCenter([0,0],100,0,400,400,1)[0]));
  assert.equal(mapZoom(100),6);assert.equal(mapZoom(0),.85);
});

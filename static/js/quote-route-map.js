/* Local geographic illustration. Never writes quote fields or requests a tariff. */
(function () {
  'use strict';
  function normalize(value) {
    return String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '')
      .toLocaleLowerCase('es').replace(/[^a-z0-9]+/g, ' ').trim();
  }
  function resolveCity(value, cities) {
    var key = normalize(value);
    if (!key) return null;
    var matches = (cities || []).filter(function (city) {
      return city.aliases.some(function (name) { return normalize(name) === key; });
    });
    // Homonyms are not guessed from population or list order.
    return matches.length === 1 ? matches[0] : null;
  }
  function resolveLocation(country, city, countries, cities) {
    if (!countries[country]) return null;
    var match = resolveCity(city, cities);
    return {point: match ? match.point : countries[country].center,
      city: match ? match.name : '', country: country, precise: Boolean(match),
      unresolved: Boolean(String(city || '').trim()) && !match};
  }
  function routeCenter(a, b, midpoint) {
    function vector(p) {var lon=p[0]*Math.PI/180,lat=p[1]*Math.PI/180;return [Math.cos(lat)*Math.cos(lon),Math.cos(lat)*Math.sin(lon),Math.sin(lat)];}
    var av=vector(a),bv=vector(b),mv=vector(midpoint);
    var normal=[av[1]*bv[2]-av[2]*bv[1],av[2]*bv[0]-av[0]*bv[2],av[0]*bv[1]-av[1]*bv[0]];
    var length=Math.hypot(...normal);if(length<.00001)return midpoint;
    // Tilt perpendicular to the route: both endpoints remain on the visible hemisphere.
    return [-1,1].map(function(sign){
      var v=mv.map(function(x,i){return x*Math.cos(.38)+sign*normal[i]/length*Math.sin(.38);});
      return [Math.atan2(v[1],v[0])*180/Math.PI,Math.asin(v[2])*180/Math.PI];
    }).sort(function(x,y){return Math.abs(x[1])-Math.abs(y[1]) || x[0]-y[0];})[0];
  }
  function resolveProvince(code, data) {
    var province=data && data.provinces[code];
    return province ? {point:province.center,city:province.name,country:'AR',province:code,precise:false} : null;
  }
  function dragCenter(center, dx, dy, width, height, zoom) {
    var sensitivity=180/(Math.max(100,Math.min(width,height))*Math.max(.85,zoom));
    var longitude=((center[0]-dx*sensitivity+180)%360+360)%360-180;
    return [longitude,Math.max(-85,Math.min(85,center[1]+dy*sensitivity))];
  }
  function mapZoom(value) {return Math.max(.85,Math.min(6,value));}
  if (typeof module !== 'undefined'  && module.exports) module.exports = {normalize:normalize,resolveCity:resolveCity,resolveLocation:resolveLocation,routeCenter:routeCenter,resolveProvince:resolveProvince,dragCenter:dragCenter,mapZoom:mapZoom};
  if (typeof window === 'undefined') return;

  var instances=new WeakMap(), allInstances=new Set(), resources, provinceFile, cityFiles=new Map(), serial=0, handoffViews=new WeakMap();
  var WORLD=[-25,18], ARGENTINA=[-64,-38], ARGENTINA_ZOOM=3.35;
  function json(url) {return fetch(url,{credentials:'same-origin'}).then(function(r){if(!r.ok)throw new Error('Map data unavailable');return r.json();});}
  function assets() {
    if(!resources)resources=Promise.all([import('/static/vendor/quote-map/geo.js?v=1'),json('/static/data/quote-map/world.json?v=1')])
      .catch(function(error){resources=null;throw error;});
    return resources;
  }
  function provinces() {
    if(!provinceFile)provinceFile=json('/static/data/quote-map/argentina.json?v=1').catch(function(error){provinceFile=null;throw error;});
    return provinceFile;
  }
  function cities(code,world) {
    if(!world.countries[code] || !world.countries[code].cities)return Promise.resolve([]);
    if(!cityFiles.has(code))cityFiles.set(code,json('/static/data/quote-map/cities/'+code+'.json?v=1').catch(function(error){cityFiles.delete(code);throw error;}));
    return cityFiles.get(code);
  }
  function selectedName(select) {
    var option=select && select.options[select.selectedIndex];
    return option && select.value ? option.textContent.replace(/\s*\([A-Z]{2}\)\s*$/,'') : '';
  }
  function create(root) {
    var box=root.querySelector('[data-quote-map]'),entry=root.classList.contains('quote-entry-screen'),national=root.dataset.quoteActive==='nacional'||root.classList.contains('national-quote-screen');
    var mode=entry?'':national?'nacional':'internacional',form=root.querySelector(root.classList.contains('unified-quote')?'[data-unified-form="'+mode+'"]':'form');
    if(!box || instances.has(root) || (!entry&&!form) || !window.IntersectionObserver || !window.ResizeObserver)return;
    var canvas=box.querySelector('canvas'),ctx=canvas.getContext('2d');if(!ctx)return;
    var f=entry?{}:{origin:form.querySelector(national?'[name="origen_provincia"]':'[name="origen_pais"]'),
      destination:form.querySelector(national?'[name="destino_provincia"]':'[name="destino_pais"]'),
      originCity:form.querySelector('[name="origen_ciudad"]'),destinationCity:form.querySelector('[name="destino_ciudad_internacional"]')};
    if(!entry&&(!f.origin||!f.destination))return;
    var handoff=handoffViews.get(root),initial=handoff && handoff.mode===mode ? handoff : null;
    handoffViews.delete(root);
    var state={geo:null,world:null,provinces:null,center:initial?initial.center:WORLD.slice(),zoom:initial?initial.zoom:.95,
      route:[],key:'',frame:0,timer:0,version:0,visible:false,collapsed:false,width:0,height:0,progress:1,loading:null,disposed:false,finish:null,lastSide:null,drag:null,paintFrame:0,manual:false,interactionVersion:0,entering:null};
    var art=box.querySelector('.quote-map-art'),resetButton=box.querySelector('[data-map-reset]'),hint=box.querySelector('[data-map-drag-hint]');
    if(entry){resetButton.setAttribute('aria-label','Volver a la vista inicial');resetButton.title='Vista inicial';}
    var reduce=window.matchMedia('(prefers-reduced-motion: reduce)'),id=++serial;
    var glint=box.querySelector('[data-map-glint]'),glintLand=box.querySelector('[data-map-glint-land]'),glintRoute=box.querySelector('[data-map-glint-route]');
    var currents=box.querySelectorAll('[data-map-current]');
    if(glint){
      var gradientId='quote-glint-gradient-'+id,maskId='quote-glint-mask-'+id;
      box.querySelector('[data-map-glint-gradient]').id=gradientId;box.querySelector('[data-map-glint-mask]').id=maskId;
      box.querySelector('[data-map-glint-layer]').setAttribute('mask','url(#'+maskId+')');
      box.querySelector('[data-map-glint-band]').setAttribute('fill','url(#'+gradientId+')');
    }
    if(!entry&&!national&&!root.classList.contains('unified-quote'))['origin','destination'].forEach(function(side){
      var list=document.createElement('datalist');list.id='quote-map-cities-'+id+'-'+side;
      f[side+'City'].setAttribute('list',list.id);form.appendChild(list);f[side+'List']=list;
    });
    box.hidden=false;root.classList.add('has-route-map');
    if(national){box.querySelector('.quote-map-heading').lastChild.textContent=' TU ENVÍO, EN ARGENTINA';box.querySelector('.quote-map-credit').textContent='Cartografía · IGN / Georef · Natural Earth';}
    var themeColors=null;
    function palette(){
      var theme=document.documentElement.dataset.theme;
      if(themeColors&&themeColors.theme===theme)return themeColors;
      var tokens=getComputedStyle(box);
      themeColors=theme==='light'
        ?{ocean:'#ffffff',oceanEdge:'#cfc7e1',land:'#afa2c7',landEdge:'#77658f',border:'rgba(102,76,143,.38)',shine:'rgba(255,255,255,.34)',shade:'rgba(50,27,89,.38)'}
        :{ocean:'#191925',oceanEdge:'#090b13',land:'#625b71',landEdge:'#383447',border:'rgba(175,157,206,.16)',shine:'rgba(222,210,244,.045)',shade:'rgba(1,3,10,.60)'};
      // Use the saturated stop from the sidebar SOLUTIONS metallic gradient.
      themeColors.theme=theme;
      themeColors.accent=tokens.getPropertyValue('--map-route-accent').trim();
      themeColors.secondary=tokens.getPropertyValue('--map-origin-accent').trim();
      themeColors.selected=themeColors.accent;
      themeColors.selectedDeep=tokens.getPropertyValue('--map-country-deep').trim();
      return themeColors;
    }
    function canDraw(){return state.geo&&state.world&&state.visible&&!state.collapsed&&root.isConnected&&!document.hidden&&state.width>0&&!state.disposed;}
    function stop(){if(glint)glint.dataset.running='false';if(state.paintFrame)cancelAnimationFrame(state.paintFrame);state.paintFrame=0;if(state.frame)cancelAnimationFrame(state.frame);state.frame=0;if(state.finish){var finish=state.finish;state.finish=null;finish(false);}}
    function draw(){
      if(glint)glint.dataset.running=String(Boolean(canDraw()&&state.route.length&&!reduce.matches));
      if(!canDraw())return;
      var g=state.geo,w=state.width,h=state.height,p=palette(),r=Math.min(w*.43,h*.43)*state.zoom,cx=w/2,cy=h/2;
      var argentinaView=state.provinces&&state.zoom>1.4&&(state.entering?state.entering==='nacional':national);
      var projection=g.geoOrthographic().rotate([-state.center[0],-state.center[1]]).translate([cx,cy]).scale(r).precision(.45),path=g.geoPath(projection,ctx);
      var shinePath=glint?g.geoPath(projection):null,shineLand=[];
      ctx.clearRect(0,0,w,h);
      // A soft cast shadow separates the light sphere from the white surface.
      if(p.theme==='light'&&state.zoom<1.5){
        ctx.save();ctx.globalAlpha=Math.min(1,(1.5-state.zoom)/.35);
        ctx.translate(cx+r*.06,cy+r*1.015);ctx.scale(r*.82,r*.13);
        var cast=ctx.createRadialGradient(0,0,0,0,0,1);
        cast.addColorStop(0,'rgba(55,30,100,.22)');cast.addColorStop(1,'rgba(55,30,100,0)');
        ctx.fillStyle=cast;ctx.fillRect(-1,-1,2,2);ctx.restore();
      }
      // A soft atmosphere behind the sphere, using the same brand violet.
      var haloRadius=r*1.28,halo=ctx.createRadialGradient(cx,cy,r*.98,cx,cy,haloRadius);
      halo.addColorStop(0,p.accent);halo.addColorStop(1,'transparent');
      ctx.save();ctx.globalAlpha=p.theme==='light'?.15:.26;
      ctx.fillStyle=halo;ctx.beginPath();ctx.arc(cx,cy,haloRadius,0,2*Math.PI);ctx.fill();ctx.restore();
      var ocean=ctx.createRadialGradient(cx-r*.35,cy-r*.5,0,cx,cy,r*1.25);
      ocean.addColorStop(0,p.ocean);ocean.addColorStop(1,p.oceanEdge);
      ctx.beginPath();path({type:'Sphere'});ctx.fillStyle=ocean;ctx.fill();ctx.save();ctx.clip();
      var land=ctx.createLinearGradient(cx-r*.5,cy-r,cx+r*.6,cy+r);
      land.addColorStop(0,p.land);land.addColorStop(1,p.landEdge);
      var selectedLand=ctx.createLinearGradient(cx-r*.5,cy-r,cx+r*.6,cy+r);
      selectedLand.addColorStop(0,p.selected);selectedLand.addColorStop(1,p.selectedDeep);
      // A single land fill avoids seams between countries. No latitude/longitude grid.
      ctx.beginPath();
      state.world.features.forEach(function(feature){
        if(state.provinces && state.zoom>1.4 && feature.id==='AR')return;
        path(feature);
      });
      ctx.save();
      if(p.theme==='light'&&!argentinaView){ctx.shadowColor='rgba(45,22,83,.22)';ctx.shadowBlur=1.5;ctx.shadowOffsetY=.8;}
      ctx.fillStyle=argentinaView?(p.theme==='light'?'#ded8e8':'#252231'):land;ctx.fill();ctx.restore();
      state.world.features.forEach(function(feature){
        if(state.provinces && state.zoom>1.4 && feature.id==='AR')return;
        if(!state.route.some(function(location){return location.country===feature.id;}))return;
        ctx.beginPath();path(feature);ctx.fillStyle=selectedLand;ctx.fill();
        if(shinePath)shineLand.push(shinePath(feature)||'');
      });
      if(state.provinces&&state.zoom>1.4){
        state.provinces.features.forEach(function(feature){
          var selected=state.route.find(function(location){return location.province===feature.id;});
          ctx.beginPath();path(feature);ctx.fillStyle=selected?selectedLand:argentinaView?(p.theme==='light'?'#b6a1df':'#463267'):land;ctx.fill();
          if(selected&&shinePath)shineLand.push(shinePath(feature)||'');
          ctx.strokeStyle=selected?(selected.side==='origin'?p.secondary:p.accent):argentinaView?'rgba(175,145,234,.45)':p.border;ctx.lineWidth=selected?1:argentinaView?.75:.5;ctx.stroke();
        });
      }
      var shade=ctx.createRadialGradient(cx-r*.35,cy-r*.4,0,cx+r*.28,cy+r*.25,r*1.4);
      shade.addColorStop(0,p.shine);shade.addColorStop(.45,'rgba(0,0,0,0)');shade.addColorStop(1,p.shade);
      ctx.fillStyle=shade;ctx.fillRect(0,0,w,h);ctx.restore();
      ctx.beginPath();path({type:'Sphere'});
      if(p.theme==='light'){
        var rim=ctx.createLinearGradient(cx-r,cy-r,cx+r,cy+r);
        rim.addColorStop(0,'rgba(255,255,255,.95)');rim.addColorStop(.4,'rgba(188,172,218,.5)');rim.addColorStop(1,'rgba(103,76,147,.65)');
        ctx.strokeStyle=rim;ctx.lineWidth=1;
      }else{ctx.strokeStyle=p.border;ctx.lineWidth=.5;}
      ctx.stroke();
      if(state.route.length===2){
        var travel=g.geoInterpolate(state.route[0].point,state.route[1].point),samples=[];
        for(var n=0;n<=96;n++)samples.push(travel(n/96*state.progress));
        var line={type:'LineString',coordinates:samples};
        if(shinePath){var routeShape=shinePath(line)||'';glintRoute.setAttribute('d',routeShape);currents.forEach(function(current){current.setAttribute('d',routeShape);});}
        ctx.beginPath();path(line);ctx.strokeStyle=p.accent;ctx.lineWidth=1.5;ctx.lineCap='round';
        ctx.shadowColor=p.accent;ctx.shadowBlur=5;ctx.stroke();ctx.shadowBlur=0;
        var head=travel(state.progress);
        if(state.progress<.99&&g.geoDistance(state.center,head)<Math.PI/2){var lead=projection(head);ctx.fillStyle=p.accent;ctx.beginPath();ctx.arc(lead[0],lead[1],3.5,0,Math.PI*2);ctx.fill();}
      }
      state.route.forEach(function(location,index){
        if(g.geoDistance(state.center,location.point)>Math.PI/2-.008)return;
        var point=projection(location.point),color=location.side==='origin'?p.secondary:p.accent;
        ctx.beginPath();ctx.arc(point[0],point[1],6,0,2*Math.PI);ctx.strokeStyle=color;ctx.globalAlpha=.3;ctx.lineWidth=1;ctx.stroke();ctx.globalAlpha=1;
        ctx.beginPath();ctx.arc(point[0],point[1],3,0,2*Math.PI);ctx.fillStyle=color;ctx.fill();
        if(national){ctx.font='10px sans-serif';ctx.fillStyle=p.accent;ctx.textAlign='left';ctx.fillText(location.province==='C'?'CABA':location.city,Math.min(w-100,Math.max(6,point[0]+12)),Math.min(h-12,Math.max(14,point[1]-10)));}
      });
      if(glint){
        glint.setAttribute('viewBox','0 0 '+w+' '+h);glintLand.setAttribute('d',shineLand.join(''));
        if(state.route.length!==2){glintRoute.setAttribute('d','');currents.forEach(function(current){current.setAttribute('d','');});}
      }
    }
    function queueDraw(){if(!state.paintFrame)state.paintFrame=requestAnimationFrame(function(){state.paintFrame=0;draw();});}
    function freeView(){
      state.manual=true;state.interactionVersion++;state.progress=1;
      state.target={center:state.center.slice(),zoom:state.zoom};
      box.dataset.mapView='manual';hint.textContent='Vista libre';
      box.querySelector('[data-map-zoom="in"]').disabled=state.zoom>=6;
      box.querySelector('[data-map-zoom="out"]').disabled=state.zoom<=.85;
    }
    function guidedView(){
      state.manual=false;box.dataset.mapView='route';hint.textContent='Arrastrá para girar';
      box.querySelectorAll('[data-map-zoom]').forEach(function(button){button.disabled=false;});
    }
    function endDrag(){
      var drag=state.drag;state.drag=null;art.classList.remove('is-dragging');
      if(drag&&canvas.hasPointerCapture(drag.id))canvas.releasePointerCapture(drag.id);
    }
    function pointerDown(event){
      if(event.button!==0||event.isPrimary===false||!canDraw()||root.classList.contains('is-choosing'))return;
      stop();state.drag={id:event.pointerId,x:event.clientX,y:event.clientY,center:state.center.slice()};
      canvas.setPointerCapture(event.pointerId);canvas.focus({preventScroll:true});art.classList.add('is-dragging','is-pointer-view');
      freeView();draw();
    }
    function pointerMove(event){
      var drag=state.drag;if(!drag||event.pointerId!==drag.id)return;
      state.center=dragCenter(drag.center,event.clientX-drag.x,event.clientY-drag.y,state.width,state.height,state.zoom);
      freeView();queueDraw();
    }
    function pointerEnd(event){if(state.drag&&event.pointerId===state.drag.id)endDrag();}
    function zoomBy(factor){if(!canDraw())return;endDrag();stop();state.zoom=mapZoom(state.zoom*factor);freeView();draw();}
    function recenter(){
      if(!state.geo)return;endDrag();guidedView();
      var route=state.route,target=national?ARGENTINA:route.length===2?routeCenter(route[0].point,route[1].point,state.geo.geoInterpolate(route[0].point,route[1].point)(.5)):route.length?route[0].point:WORLD;
      animate([{center:target,zoom:national?ARGENTINA_ZOOM:1,duration:800,trace:true}]);
    }
    function keyDown(event){
      art.classList.remove('is-pointer-view');if(!canDraw())return;
      var dx=event.key==='ArrowRight'?24:event.key==='ArrowLeft'?-24:0,dy=event.key==='ArrowDown'?24:event.key==='ArrowUp'?-24:0;
      if(dx||dy){event.preventDefault();endDrag();stop();state.center=dragCenter(state.center,dx,dy,state.width,state.height,state.zoom);freeView();draw();}
      else if(['+','=','-','Home'].includes(event.key)){event.preventDefault();if(event.key==='Home')recenter();else zoomBy(event.key==='-'?1/1.25:1.25);}
    }
    canvas.addEventListener('pointerdown',pointerDown);canvas.addEventListener('pointermove',pointerMove);
    canvas.addEventListener('pointerup',pointerEnd);canvas.addEventListener('pointercancel',pointerEnd);canvas.addEventListener('lostpointercapture',pointerEnd);
    canvas.addEventListener('keydown',keyDown);
    resetButton.addEventListener('click',recenter);
    box.querySelector('[data-map-zoom="in"]').addEventListener('click',function(){zoomBy(1.25);});
    box.querySelector('[data-map-zoom="out"]').addEventListener('click',function(){zoomBy(1/1.25);});
    function resize(){var rect=canvas.getBoundingClientRect(),dpr=Math.min(window.devicePixelRatio||1,1.7);state.width=rect.width;state.height=rect.height;if(!rect.width||!rect.height)return;canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);draw();}
    function animate(plan){
      endDrag();stop();var last=plan[plan.length-1];state.target=last;
      if(reduce.matches||!canDraw()){state.center=last.center;state.zoom=last.zoom;state.progress=1;draw();return Promise.resolve(true);}
      return new Promise(function(resolve){
        state.finish=resolve;var index=0,start=performance.now(),from=state.center.slice(),zoom=state.zoom;
        function tick(now){
          if(!canDraw()){motion();return;}
          var step=plan[index],t=Math.min(1,(now-start)/(step.duration||1100)),ease=t*t*(3-2*t);
          state.center=state.geo.geoInterpolate(from,step.center)(ease);state.zoom=zoom+(step.zoom-zoom)*ease;
          state.progress=step.trace?ease:0;draw();
          if(t<1)state.frame=requestAnimationFrame(tick);
          else if(++index<plan.length){start=now;from=state.center.slice();zoom=state.zoom;state.frame=requestAnimationFrame(tick);}
          else{state.frame=0;state.finish=null;state.progress=1;draw();resolve(true);}
        }
        state.frame=requestAnimationFrame(tick);
      });
    }
    function motion(){var target=state.target;endDrag();stop();if(target){state.center=target.center;state.zoom=target.zoom;}state.progress=1;draw();}
    function suggest(side,rows){var query=normalize(f[side+'City'].value),list=f[side+'List'];if(!list)return;list.replaceChildren();rows.filter(function(row){return !query||row.aliases.some(function(name){return normalize(name).startsWith(query);});}).slice(0,8).forEach(function(row){var option=document.createElement('option');option.value=row.name;list.appendChild(option);});}
    function label(side,location){
      var name=selectedName(f[side]);
      box.querySelector('[data-map-'+side+']').textContent=location&&(national||location.precise)?(location.province==='C'?'CABA':location.city):name||(side==='origin'?'Elegí origen':'Elegí destino');
      box.querySelector('[data-map-'+side+'-country]').textContent=national?(location?'Referencia provincial':''):location&&location.precise?name:name?'Vista del país':'';
    }
    async function update(side){
      if(!state.world||entry)return;var version=++state.version,interactionVersion=state.interactionVersion;
      try{
        var a,b;
        if(national){state.provinces=await provinces();a=resolveProvince(f.origin.value,state.provinces);b=resolveProvince(f.destination.value,state.provinces);}
        else{var rows=await Promise.all([cities(f.origin.value,state.world),cities(f.destination.value,state.world)]);
          if(version!==state.version||state.disposed)return;
          suggest('origin',rows[0]);suggest('destination',rows[1]);a=resolveLocation(f.origin.value,f.originCity.value,state.world.countries,rows[0]);b=resolveLocation(f.destination.value,f.destinationCity.value,state.world.countries,rows[1]);}
        if(version!==state.version||state.disposed)return;
        if(a)a.side='origin';if(b)b.side='destination';state.route=[a,b].filter(Boolean);label('origin',a);label('destination',b);
        var unknown=[a,b].some(function(l){return l&&l.unresolved;});
        box.querySelector('[data-map-caption]').textContent=national?'Referencia entre provincias · sin validar cobertura.':unknown?'Ciudad sin ubicar · mostramos el país.':a&&b&&a.precise&&b.precise?'Conexión orientativa de origen a destino.':'Conexión orientativa entre países.';
        var key=state.route.map(function(l){return l.country+':'+l.point.join(',');}).join('|');
        if(key===state.key&&state.target){draw();return;}state.key=key;
        var target=national?ARGENTINA:a&&b?routeCenter(a.point,b.point,state.geo.geoInterpolate(a.point,b.point)(.5)):side&&(a||b)?(a||b).point:WORLD;
        var plan=[],focused=side==='origin'?a:side==='destination'?b:null;
        if(focused)plan.push({center:focused.point,zoom:national?4.5:1.3,duration:850,trace:false});
        if(a&&b||!focused)plan.push({center:target,zoom:national?ARGENTINA_ZOOM:1,duration:1250,trace:true});
        else plan[0].trace=true;
        if(state.drag||interactionVersion!==state.interactionVersion){draw();return;}
        guidedView();animate(plan);
      }catch(error){if(version===state.version){stop();state.route=[];state.key='';label('origin',null);label('destination',null);draw();box.querySelector('[data-map-caption]').textContent='El detalle del mapa no está disponible. Podés seguir cargando.';}}
    }
    function schedule(event){
      if(state.entering)return;
      var side=event.target===f.origin||event.target===f.originCity?'origin':event.target===f.destination||event.target===f.destinationCity?'destination':null;
      if(!side)return;
      state.version++;clearTimeout(state.timer);state.lastSide=side;state.timer=setTimeout(function(){update(side);},event.type==='input'?420:0);
    }
    async function start(){
      if(state.world)return;if(state.loading)return state.loading;
      state.loading=(async function(){try{var loaded=await assets();if(state.disposed)return;state.geo=loaded[0];state.world=loaded[1];resize();
        if(entry)await animate([{center:[-15,8],zoom:1,duration:1800,trace:true}]);else await update();
      }catch(error){box.querySelector('[data-map-caption]').textContent='El mapa no está disponible. Podés seguir cotizando.';}finally{state.loading=null;}})();
      return state.loading;
    }
    if(form){form.addEventListener('input',schedule);form.addEventListener('change',schedule);}
    box.querySelector('[data-map-toggle]').addEventListener('click',function(){state.collapsed=!state.collapsed;root.classList.toggle('quote-map-is-off',state.collapsed);this.textContent=state.collapsed?'Mostrar mapa':'Ocultar mapa';this.setAttribute('aria-pressed',String(state.collapsed));motion();if(!state.collapsed){resize();update();}});
    var observer=new IntersectionObserver(function(entries){state.visible=entries[0].isIntersecting;if(state.visible){start();resize();}else motion();},{rootMargin:'80px'});observer.observe(box);
    var resizeObserver=new ResizeObserver(resize);resizeObserver.observe(canvas);
    var themeObserver=new MutationObserver(draw);themeObserver.observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});reduce.addEventListener('change',motion);
    var instance={root:root,stop:motion,draw:draw,refresh:function(){handoffViews.delete(root);state.key='';guidedView();if(entry)animate([{center:WORLD,zoom:1,duration:800,trace:true}]);else update();},enter:async function(scope){
      guidedView();if(!state.world){await start();}if(!state.geo)return;
      state.entering=scope;state.version++;clearTimeout(state.timer);state.route=[];
      hint.textContent=scope==='nacional'?'Acercando a Argentina…':'Volviendo al mundo…';
      if(scope==='nacional'){try{state.provinces=await provinces();}catch(error){/* The form still opens without map detail. */}}
      var target={center:scope==='nacional'?ARGENTINA:WORLD,zoom:scope==='nacional'?ARGENTINA_ZOOM:1,duration:1150,trace:true};
      // First face Argentina at world scale, then move into its provinces.
      var plan=scope==='nacional'?[{center:ARGENTINA,zoom:1,duration:950,trace:false},target]:[target];
      await animate(plan);handoffViews.set(root,{mode:scope,center:target.center.slice(),zoom:target.zoom});
    },dispose:function(){state.disposed=true;state.version++;endDrag();stop();canvas.removeEventListener('pointerdown',pointerDown);canvas.removeEventListener('pointermove',pointerMove);canvas.removeEventListener('pointerup',pointerEnd);canvas.removeEventListener('pointercancel',pointerEnd);canvas.removeEventListener('lostpointercapture',pointerEnd);canvas.removeEventListener('keydown',keyDown);resetButton.removeEventListener('click',recenter);clearTimeout(state.timer);observer.disconnect();resizeObserver.disconnect();themeObserver.disconnect();reduce.removeEventListener('change',motion);if(form){form.removeEventListener('input',schedule);form.removeEventListener('change',schedule);}allInstances.delete(instance);instances.delete(root);}};
    instances.set(root,instance);allInstances.add(instance);
  }
  function attach(root){var screens=root.matches&&root.matches('.quote-screen')?[root]:Array.from(root.querySelectorAll('.quote-screen'));screens.forEach(create);}
  function dispose(root){var instance=root&&instances.get(root);if(instance)instance.dispose();}
  document.addEventListener('visibilitychange',function(){allInstances.forEach(function(i){if(document.hidden)i.stop();else i.draw();});});
  window.TauroQuoteMap={attach:attach,dispose:dispose,refresh:function(root){var instance=instances.get(root);if(instance)instance.refresh();},transition:function(root,scope){var instance=instances.get(root);return instance?instance.enter(scope):Promise.resolve();}};
  attach(document);
})();

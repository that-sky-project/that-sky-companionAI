export function box(cx,cy,cz,hx,hy,hz){
  const p=[];for(const [x,y,z] of [[-1,-1,-1],[1,-1,-1],[1,-1,1],[-1,-1,1],[-1,1,-1],[1,1,-1],[1,1,1],[-1,1,1]])p.push(cx+x*hx,cy+y*hy,cz+z*hz);
  const t=[0,1,2,0,2,3,4,6,5,4,7,6,0,4,5,0,5,1,1,5,6,1,6,2,2,6,7,2,7,3,3,7,4,3,4,0];
  return {positions:p,indices:t};
}
export function merge(...meshes){const p=[],t=[];for(const m of meshes){const n=p.length/3;p.push(...m.positions);t.push(...m.indices.map(i=>i+n));}return {positions:new Float32Array(p),indices:new Uint32Array(t)};}
export const floor=()=>box(0,-.25,0,10,.25,10);
export const wall=()=>box(0,1.5,0,.15,1.5,2);
export const ramp=(height=2)=>({positions:[-3,0,-2,3,0,-2,3,height,2,-3,height,2],indices:[0,2,1,0,3,2]});

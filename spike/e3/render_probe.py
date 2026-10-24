import os, mujoco, numpy as np
os.environ.setdefault('MUJOCO_GL','egl')
m=mujoco.MjModel.from_xml_path('vendor/mujoco_menagerie/unitree_g1/scene.xml')
d=mujoco.MjData(m); mujoco.mj_resetDataKeyframe(m,d,0); mujoco.mj_forward(m,d)
for gid in range(m.ngeom):
    mid=m.geom_dataid[gid]
    if mid in (16,18,26,34):
        print('gid',gid,'meshid',mid,'xpos',np.round(d.geom_xpos[gid],4),'xmat',np.round(d.geom_xmat[gid].reshape(3,3),3))
r=mujoco.Renderer(m,height=480,width=640)
cam=mujoco.MjvCamera(); cam.type=mujoco.mjtCamera.mjCAMERA_FREE; cam.lookat[:]=[0,0,0.9]; cam.distance=2.2; cam.azimuth=140; cam.elevation=-15
r.update_scene(d,camera=cam)
img=r.render()
import imageio.v3 as iio
iio.imwrite('evidence/sim_stand.png',img)
print('wrote evidence/sim_stand.png', img.shape, img.dtype)

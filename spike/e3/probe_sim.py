import mujoco, numpy as np
m = mujoco.MjModel.from_xml_path('vendor/mujoco_menagerie/unitree_g1/scene.xml')
d = mujoco.MjData(m)
print('nq',m.nq,'nv',m.nv,'nu',m.nu,'nbody',m.nbody,'njnt',m.njnt)
for i in range(m.njnt):
    print(i, m.joint(i).name, 'qposadr',m.jnt_qposadr[i],'dofadr',m.jnt_dofadr[i])
print('keyframes', m.nkey)
mujoco.mj_resetDataKeyframe(m,d,0)
mujoco.mj_forward(m,d)
for name in ['pelvis','waist_yaw_link','torso_link','left_wrist_yaw_link','right_wrist_yaw_link']:
    b=d.body(name)
    print('INIT',name,'pos',np.round(b.xpos,4),'quat(wxyz)',np.round(b.xquat,4))
print('ctrl',np.round(d.ctrl,3))
# step 2 seconds
for i in range(1000):
    mujoco.mj_step(m,d)
print('after 2s sim time',d.time)
print('base pos',np.round(d.qpos[:3],4),'base quat',np.round(d.qpos[3:7],4))
for name in ['pelvis','torso_link','left_wrist_yaw_link','right_wrist_yaw_link']:
    b=d.body(name)
    print('END',name,'pos',np.round(b.xpos,4),'quat(wxyz)',np.round(b.xquat,4))
print('qpos',np.round(d.qpos,3))
print('ctrl',np.round(d.ctrl,3))

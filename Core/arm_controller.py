import rospy
from al5d_gazebo.msg import TransformStampedList
from std_msgs.msg import Float64
from sensor_msgs.msg import JointState
import threading
from Queue import Queue
import numpy as np
from tf.transformations import quaternion_matrix
from copy import deepcopy
np.set_printoptions(suppress=True)

#class managing separate ROS loop
class ROSInterface:
    def __init__(self, cmd_q, status_q, pose_q, num_joints):
        #load up queues for async comm
        self.cmd_q = cmd_q
        self.status_q = status_q
        self.pose_q = pose_q
        self.num_joints = num_joints
        self.pos = None
        self.vel = None
        self.move_ind = 0
        self.move_seq = 0
        
    def state_cb(self, state):
        #add state to queue
        #note in original message joints are in alphabetical order
        #here we reorder to agree with command format
        self.pos = np.array([state.position[0],
                             state.position[4],
                             state.position[1],
                             state.position[6],
                             state.position[5],
                             state.position[2]])
        self.vel = np.array([state.velocity[0],
                             state.velocity[4],
                             state.velocity[1],
                             state.velocity[6],
                             state.velocity[5],
                             state.position[2]])

        #clear queue
        while not self.status_q.empty():
            self.status_q.get()
        self.status_q.put((self.pos, self.vel))

    def pose_cb(self, pose):
        #we know the transforms are already in the order we want
        transforms = []
        for trans in pose.transforms:
            translation = 1000*np.array([trans.transform.translation.x,
                                         trans.transform.translation.y,
                                         trans.transform.translation.z])



            matrix = quaternion_matrix([trans.transform.rotation.x,
                                           trans.transform.rotation.y,
                                           trans.transform.rotation.z,
                                           trans.transform.rotation.w])
            matrix[:3,3] = translation
            transforms.append(matrix)

        #clear queue
        while not self.pose_q.empty():
            self.pose_q.get()
        self.pose_q.put(transforms)

    def start_command(self, state):
        if state[0] == "move":
            if self.pos is not None:
                #interpolate states between current state and goal
                diff = state[1] - self.pos
                dist = np.max(np.abs(diff))
                interp = np.linspace(0, 1, max(np.ceil(100*dist), 2))
                self.move_seq = self.pos + interp[:,None]*diff[None,:]
                self.move_ind = 0

    def update_move(self):
        if self.move_seq is not 0:
            #if we are interpolating, move to next state
            if self.move_ind < self.move_seq.shape[0]:
                self.set_state(self.move_seq[self.move_ind,:])
                self.move_ind += 1
            else:
                self.move_ind = 0
                self.move_seq = 0

    def set_state(self, state):
        
        for ind, s in enumerate(state[:-1]):
            msg = Float64(s)
            self.pos_pubs[ind].publish(msg)

        self.gripper_pubs[0].publish(Float64(-state[-1]))
        self.gripper_pubs[1].publish(Float64(state[-1]))

    def loop(self):
        node = rospy.init_node('arm_controller', disable_signals=True)
        self.pos_pubs = []
        self.gripper_pubs = []
        for i in range(self.num_joints):
            pub = rospy.Publisher("/al5d_arm_position_controller"+str(i+1)+"/command", 
                                  Float64, queue_size=1, latch=True)
            self.pos_pubs.append(pub)
        for i in range(2):
            pub = rospy.Publisher("/al5d_gripper_controller"+str(i+1)+"/command", 
                                  Float64, queue_size=1, latch=True)
            self.gripper_pubs.append(pub)

        state_sub = rospy.Subscriber("/joint_states", JointState, self.state_cb)
        pose_sub = rospy.Subscriber("/joint_poses", TransformStampedList, self.pose_cb)

        #poll at 100Hz
        rate = rospy.Rate(50)

        while not rospy.is_shutdown():
            try:
                if not self.status_q.empty() and not self.pose_q.empty():
                    if not self.cmd_q.empty():
                        cmd = self.cmd_q.get() 
                        if cmd == "stop": 
                            break 
                        print(cmd)
                        self.start_command(cmd)
                    self.update_move()
                else:
                    rospy.loginfo("Waiting for Gazebo...")
                rate.sleep()
            except KeyboardInterrupt:
                break

class ArmController:
    def __init__(self, num_joints=5):
        self.num_joints = num_joints
        self.cur_state = ()
        self.cur_pose = ()
        self.cmd_q = Queue()
        self.status_q = Queue()
        self.pose_q = Queue()

        self.joint_limits = np.array([[-1.4, 1.4],
                                      [-1.2, 1.4],
                                      [-1.8, 1.7],
                                      [-1.9, 1.7],
                                      [-2, 1.5],
                                      [-15, 30]]).T

        #ROS runs in a separate thread because it needs to always
        #check the message buffers
        rospy.loginfo("Starting ros thread...")
        self.ros = ROSInterface(self.cmd_q, self.status_q, self.pose_q, num_joints)
        self.spin_t = threading.Thread(target=self.ros.loop)
        self.spin_t.start()
        rospy.loginfo("Ros thread started")

    def set_state(self, state):
        #num joints + gripper
        if len(state) == self.num_joints + 1:
            scaled_state = deepcopy(state)
            #check limits
            if np.any(scaled_state < self.joint_limits[0]):
                bad_joints = np.where(scaled_state < self.joint_limits[0])[0]
                for bad_joint in bad_joints:
                    rospy.logwarn("Joint " + str(bad_joint) + " is below the limit " + 
                          str(self.joint_limits[0, bad_joint]))
                scaled_state = np.maximum(scaled_state, self.joint_limits[0])
            if np.any(scaled_state > self.joint_limits[1]):
                bad_joints = np.where(scaled_state > self.joint_limits[1])[0]
                for bad_joint in bad_joints:
                    rospy.logwarn("Joint " + str(bad_joint) + " is above the limit " + 
                          str(self.joint_limits[1, bad_joint]))
                scaled_state = np.minimum(scaled_state, self.joint_limits[1])
            scaled_state[-1] = (-scaled_state[-1]+30.)/45.*0.03
            self.cmd_q.put(("move", scaled_state))
        else:
            raise Exception("Invalid state command")

    def get_state(self):

        # output: the joint value and its velocity
        if not self.status_q.empty():
            self.cur_state = self.status_q.queue[0]


        scaled_state = []
        scaled_state.append(deepcopy(self.cur_state[0]))
        scaled_state.append(deepcopy(self.cur_state[1]))
        scaled_state[0][-1] = -scaled_state[0][-1]*45./0.03 + 30.
        scaled_state[1][-1] = -scaled_state[1][-1]*45./0.03

        pos = np.around(scaled_state[0], decimals=3).tolist()
        vel = np.around(scaled_state[1], decimals=3).tolist()
       
        return pos, vel
    
    def get_poses(self):

        # output: the T matrix of each joint  
        if not self.pose_q.empty():
            self.cur_pose = self.pose_q.queue[0]

        return np.around(self.cur_pose, decimals=3)
    
    def stop(self):
        self.cmd_q.put("stop")
        self.spin_t.join()

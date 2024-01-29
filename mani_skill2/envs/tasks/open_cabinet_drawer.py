from collections import OrderedDict
from typing import Any, Dict, List

import numpy as np
import sapien
import torch

from mani_skill2.envs.sapien_env import BaseEnv
from mani_skill2.sensors.camera import CameraConfig
from mani_skill2.utils.building.actors import build_sphere
from mani_skill2.utils.building.articulations import (
    MODEL_DBS,
    _load_partnet_mobility_dataset,
    build_preprocessed_partnet_mobility_articulation,
)
from mani_skill2.utils.building.ground import build_tesselated_square_floor
from mani_skill2.utils.geometry.geometry import transform_points
from mani_skill2.utils.geometry.trimesh_utils import (
    get_render_shape_meshes,
    merge_meshes,
)
from mani_skill2.utils.registration import register_env
from mani_skill2.utils.sapien_utils import look_at, to_tensor
from mani_skill2.utils.structs.articulation import Articulation
from mani_skill2.utils.structs.link import Link
from mani_skill2.utils.structs.pose import Pose


@register_env("OpenCabinet-v1", max_episode_steps=200)
class OpenCabinetEnv(BaseEnv):
    """
    Task Description
    ----------------
    Add a task description here

    Randomizations
    --------------

    Success Conditions
    ------------------

    Visualization: link to a video/gif of the task being solved
    """

    def __init__(
        self,
        *args,
        robot_uid="fetch",
        robot_init_qpos_noise=0.02,
        **kwargs,
    ):
        self.robot_init_qpos_noise = robot_init_qpos_noise
        _load_partnet_mobility_dataset()
        self.all_model_ids = np.array(
            list(MODEL_DBS["PartnetMobility"]["model_data"].keys())
        )
        super().__init__(*args, robot_uid=robot_uid, **kwargs)

    def _register_sensors(self):
        pose = look_at(eye=[0.3, 0, 0.6], target=[-0.1, 0, 0.1])
        return [
            CameraConfig("base_camera", pose.p, pose.q, 128, 128, np.pi / 2, 0.01, 10)
        ]

    def _register_render_cameras(self):
        pose = look_at(eye=[-2.5, -2.5, 2.5], target=[-0.1, 0, 0.1])
        return CameraConfig("render_camera", pose.p, pose.q, 512, 512, 1, 0.01, 10)

    def _load_actors(self):
        self.ground = build_tesselated_square_floor(self._scene)
        self._load_cabinets(["prismatic"])

        from mani_skill2.agents.robots.fetch import FETCH_UNIQUE_COLLISION_BIT

        # TODO (stao) (arth): is there a better way to model robots in sim. This feels very unintuitive.
        for obj in self.ground._objs:
            cs = obj.find_component_by_type(
                sapien.physx.PhysxRigidStaticComponent
            ).get_collision_shapes()[0]
            cg = cs.get_collision_groups()
            cg[2] |= FETCH_UNIQUE_COLLISION_BIT
            cg[2] |= 1 << 29  # make ground ignore collisions with kinematic objects?
            cs.set_collision_groups(cg)

    def _load_cabinets(self, joint_types: List[str]):
        rand_idx = torch.randperm(len(self.all_model_ids))
        model_ids = self.all_model_ids[rand_idx]
        model_ids = np.concatenate(
            [model_ids] * np.ceil(self.num_envs / len(self.all_model_ids)).astype(int)
        )[: self.num_envs]
        cabinets = []
        self.cabinet_heights = []
        handle_links: List[List[Link]] = []
        handle_links_meshes: List[List[Any]] = []
        for i, model_id in enumerate(model_ids):
            scene_mask = np.zeros(self.num_envs, dtype=bool)
            scene_mask[i] = True
            cabinet, metadata = build_preprocessed_partnet_mobility_articulation(
                self._scene, model_id, name=f"{model_id}-{i}", scene_mask=scene_mask
            )
            self.cabinet_heights.append(-2 * metadata.bbox.bounds[0, 2])
            handle_links.append([])
            handle_links_meshes.append([])
            # NOTE (stao): interesting future project similar to some kind of quality diversity is accelerating policy learning by dynamically shifting distribution of handles/cabinets being trained on.
            for link, joint in zip(cabinet.links, cabinet.joints):
                if joint.type[0] in joint_types:
                    handle_links[-1].append(link)
                    handle_links_meshes[-1].append(
                        link.generate_mesh(lambda _, x: "handle" in x.name, "handle")[0]
                    )
            cabinets.append(cabinet)

        # we can merge different articulations with different degrees of freedoms as done below
        # allowing you to manage all of them under one object and retrieve data like qpos, pose, etc. all together
        # and with high performance. Note that some properties such as qpos and qlimits are now padded.
        self.cabinet = Articulation.merge_articulations(cabinets, name="cabinet")

        self.cabinet_metadata = metadata
        self.handle_links = handle_links
        self.handle_link = Link.create(
            [x[0]._objs[0] for x in handle_links], self.cabinet
        )
        self.handle_links_meshes = handle_links_meshes
        self.handle_link_goal_marker = build_sphere(
            self._scene,
            radius=0.05,
            color=[0, 1, 0, 1],
            name="handle_goal_marker",
            body_type="kinematic",
            add_collision=False,
        )
        self._hidden_objects.append(self.handle_link_goal_marker)

    def _initialize_actors(self):
        with torch.device(self.device):
            # TODO (stao): sample random link objects to create a Link object

            xyz = torch.zeros((self.num_envs, 3))
            xyz[:, 2] = torch.tensor(self.cabinet_heights) / 2
            self.cabinet.set_pose(Pose.create_from_pq(p=xyz))
            # TODO (stao): surely there is a better way to transform points here?
            handle_link_positions = to_tensor(
                np.array(
                    [x[0].bounding_box.center_mass for x in self.handle_links_meshes]
                )
            ).float()  # (N, 3)
            handle_link_positions = transform_points(
                self.handle_link.pose.to_transformation_matrix(), handle_link_positions
            )

            self.handle_link_goal_marker.set_pose(
                Pose.create_from_pq(p=handle_link_positions) * self.cabinet.pose
            )
            # close all the cabinets. We know beforehand that lower qlimit means "closed" for these assets.
            qlimits = self.cabinet.get_qlimits()  # [N, self.cabinet.max_dof, 2])
            self.cabinet.set_qpos(qlimits[:, :, 0])
            # initialize robot
            if self.robot_uid == "panda":
                self.agent.robot.set_qpos(self.agent.robot.qpos * 0)
                self.agent.robot.set_pose(Pose.create_from_pq(p=[-1, 0, 0]))
            elif self.robot_uid == "fetch":
                qpos = np.array(
                    [
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        -np.pi / 4,
                        0,
                        np.pi / 4,
                        0,
                        np.pi / 3,
                        0,
                        0.015,
                        0.015,
                    ]
                )
                self.agent.reset(qpos)
                self.agent.robot.set_pose(sapien.Pose([-1.5, 0, 0]))

            # NOTE (stao): This is a temporary work around for the issue where the cabinet drawers/doors might open themselves on the first step. It's unclear why this happens on GPU sim only atm.
            self._scene._gpu_apply_all()
            self._scene.px.step()
            self.cabinet.set_qpos(qlimits[:, :, 0])

    def evaluate(self):
        return {"success": torch.zeros(self.num_envs, device=self.device, dtype=bool)}

    def _get_obs_extra(self, info: Dict):
        return OrderedDict()

    def compute_dense_reward(self, obs: Any, action: torch.Tensor, info: Dict):
        return torch.zeros(self.num_envs, device=self.device)

    def compute_normalized_dense_reward(
        self, obs: Any, action: torch.Tensor, info: Dict
    ):
        max_reward = 1.0
        return self.compute_dense_reward(obs=obs, action=action, info=info) / max_reward

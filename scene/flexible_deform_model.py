#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#

import torch
import numpy as np
from utils.general_utils import inverse_sigmoid, get_expon_lr_func, build_rotation
from torch import nn
import os
from utils.system_utils import mkdir_p
from plyfile import PlyData, PlyElement
from random import randint
from utils.sh_utils import RGB2SH, SH2RGB
from simple_knn._C import distCUDA2
from utils.graphics_utils import BasicPointCloud, getWorld2View2
from utils.general_utils import strip_symmetric, build_scaling_rotation
from scene.regulation import compute_plane_smoothness
from typing import Tuple

import cv2
import open3d as o3d
from simple_knn._C import distCUDA2

import scipy
from scipy.spatial.transform import Rotation as R

#changes made based on errors shown

SAVE_TIME = 10
INTERVAL = 0.05
MAX_NUM = int(SAVE_TIME/INTERVAL)
GAUSSIAN_NUM = 10
FOURIER_ORDER_NUM = 10
CH_NUM = 13
CURVE_NUM = 20
    
class GaussianModel:

    def setup_functions(self):
        return

    def build_covariance_from_scaling_rotation(scaling, scaling_modifier, rotation):
        L = build_scaling_rotation(scaling_modifier * scaling, rotation)
        actual_covariance = L @ L.transpose(1, 2)
        symm = strip_symmetric(actual_covariance)
        return symm
        
        self.scaling_activation = torch.exp
        self.scaling_inverse_activation = torch.log
        self.covariance_activation = build_covariance_from_scaling_rotation
        self.opacity_activation = torch.sigmoid
        self.inverse_opacity_activation = inverse_sigmoid
        self.rotation_activation = torch.nn.functional.normalize

    def __init__(self, sh_degree : int, args, config=None):
        self.active_sh_degree = 0
        self.max_sh_degree = sh_degree
        # self.max_sh_degree = 0

        self._xyz = torch.empty(0, device="cuda")
        self._features_dc = torch.empty(0, device="cuda")
        self._features_rest = torch.empty(0, device="cuda")
        self._scaling = torch.empty(0, device="cuda")
        self._rotation = torch.empty(0, device="cuda")
        self._opacity = torch.empty(0, device="cuda")
        self._coefs = torch.empty(0, device="cuda")
        self.max_radii2D = torch.empty(0, device="cuda")
        self.xyz_gradient_accum = torch.empty(0, device="cuda")

        self.unique_kfIDs = torch.empty(0).int()
        self.n_obs = torch.empty(0).int()

        self.config = config
        self.ply_input = None
        self.args = args
        
        self.gm_num = 8
        self.ch_num = CH_NUM
        self.fs_num = FOURIER_ORDER_NUM
        
        self.save_coef_path = None

        self.start_time = None
        self.init_param = 0.03
        self.min_sigma2 = 1e-4

        self.optimizer = None

        self.scaling_activation = lambda x: torch.clip(torch.exp(x), max=2)
        # self.scaling_activation = torch.exp
        self.scaling_inverse_activation = torch.log

        self.covariance_activation = self.build_covariance_from_scaling_rotation

        self.opacity_activation = torch.sigmoid
        self.inverse_opacity_activation = inverse_sigmoid

        self.rotation_activation = torch.nn.functional.normalize

        self.isotropic = False
        
        self.start_time = None
        self.time = None
        self.max_time = 1.
        self.init_param = 0.01
        self.clear_deformation()
        #self.training_setup() 

    def capture(self):
        return (
            self.active_sh_degree,
            self._xyz,
            self._deformation_table,
            self._features_dc,
            self._features_rest,
            self._scaling,
            self._rotation,
            self._opacity,
            self.max_radii2D,
            self.xyz_gradient_accum,
            self.denom,
            self.optimizer.state_dict(),
            self.percent_dense,
            self.spatial_lr_scale,
        )
    
    def restore(self, model_args, training_args):
        (self.active_sh_degree, 
            self._xyz, 
            self._deformation_table,
            self._features_dc, 
            self._features_rest,
            self._scaling, 
            self._rotation, 
            self._opacity,
            self.max_radii2D, 
            xyz_gradient_accum, 
            denom,
            opt_dict, 
            self.spatial_lr_scale) = model_args
        self.training_setup(training_args)
        self.xyz_gradient_accum = xyz_gradient_accum
        self.denom = denom
        self.optimizer.load_state_dict(opt_dict)

    @property
    def get_scaling(self):
        if self.deform_scaling is not None:
            return self.scaling_activation(self._scaling + self.deform_scaling)
        else:
            return self.scaling_activation(self._scaling)
        
    @property
    def get_gaussian_scaling(self):
        return self.scaling_activation(self._scaling)

    @property
    def get_rotation(self):
        if self.deform_rot is not None:
            return self.rotation_activation(self._rotation + self.deform_rot)
        else:
            return self.rotation_activation(self._rotation)
        
    @property
    def get_gaussian_rotation(self):
        return self.rotation_activation(self._rotation)

    @property
    def get_xyz(self):
        if self.deform_xyz is not None:
            return self._xyz + self.deform_xyz
        return self._xyz
    
    @property
    def get_features(self):
        features_dc = self._features_dc
        features_rest = self._features_rest
        if self.deform_brightness is None:
            return torch.cat((features_dc, features_rest), dim=1)
        else:
            return torch.cat((features_dc+self.deform_brightness, features_rest), dim=1)

    @property
    def get_opacity(self):
        if self.deform_opacity is not None:
            return self.opacity_activation(self.deform_opacity + self._opacity)
        return self.opacity_activation(self._opacity)
        
    @property
    def get_gaussian_opacity(self):
        return self.opacity_activation(self._opacity)

    def get_covariance(self, scaling_modifier=1):
        return self.covariance_activation(
            self.get_gaussian_scaling, scaling_modifier, self._rotation
        )

    def oneupSHdegree(self):
        if self.active_sh_degree < self.max_sh_degree:
            self.active_sh_degree += 1

    def create_from_pcd(self, pcd : BasicPointCloud, spatial_lr_scale : float, time_line: int):
        self.spatial_lr_scale = spatial_lr_scale
        fused_point_cloud = torch.tensor(np.asarray(pcd.points)).float().cuda()
        fused_color = RGB2SH(torch.tensor(np.asarray(pcd.colors)).float().cuda())
        features = torch.zeros((fused_color.shape[0], 3, (self.max_sh_degree + 1) ** 2)).float().cuda()
        features[:, :3, 0 ] = fused_color
        features[:, 3:, 1:] = 0.0

        print("Number of points at initialisation : ", fused_point_cloud.shape[0])

        dist2 = torch.clamp_min(distCUDA2(torch.from_numpy(np.asarray(pcd.points)).float().cuda()), 0.0000001)
        scales = torch.log(torch.sqrt(dist2))[...,None].repeat(1, 3)
        rots = torch.zeros((fused_point_cloud.shape[0], 4), device="cuda")
        rots[:, 0] = 1

        
        N = fused_point_cloud.shape[0]
        weight_coefs = torch.zeros((N, self.args.ch_num, self.args.curve_num))
        position_coefs = torch.zeros((N, self.args.ch_num, self.args.curve_num)) + torch.linspace(0,1,self.args.curve_num)
        shape_coefs = torch.zeros((N, self.args.ch_num, self.args.curve_num)) + self.args.init_param
        _coefs = torch.stack((weight_coefs, position_coefs, shape_coefs), dim=2).reshape(N,-1).float().to("cuda")
        self._coefs = nn.Parameter(_coefs.requires_grad_(True))
        
        opacities = inverse_sigmoid(0.1 * torch.ones((fused_point_cloud.shape[0], 1), dtype=torch.float, device="cuda"))

        self._xyz = nn.Parameter(fused_point_cloud.requires_grad_(True))
        self._features_dc = nn.Parameter(features[:,:,0:1].transpose(1, 2).contiguous().requires_grad_(True))
        self._features_rest = nn.Parameter(features[:,:,1:].transpose(1, 2).contiguous().requires_grad_(True))
        self._scaling = nn.Parameter(scales.requires_grad_(True))
        self._rotation = nn.Parameter(rots.requires_grad_(True))
        self._opacity = nn.Parameter(opacities.requires_grad_(True))
        self.max_radii2D = torch.zeros((self.get_xyz.shape[0]), device="cuda")
        self._deformation_table = torch.gt(torch.ones((self.get_xyz.shape[0]),device="cuda"),0)

    def training_setup(self, training_args):
        #training_args = self.config['opt_params']
        self.percent_dense = training_args.percent_dense
        self.xyz_gradient_accum = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.denom = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self._deformation_accum = torch.zeros((self.get_xyz.shape[0],3),device="cuda")
        
        l = [
            {'params': [self._xyz], 'lr': training_args.position_lr_init * self.spatial_lr_scale, "name": "xyz"},
            {'params': [self._features_dc], 'lr': training_args.feature_lr, "name": "f_dc"},
            {'params': [self._features_rest], 'lr': training_args.feature_lr / 20.0, "name": "f_rest"},
            {'params': [self._opacity], 'lr': training_args.opacity_lr, "name": "opacity"},
            {'params': [self._scaling], 'lr': training_args.scaling_lr, "name": "scaling"},
            {'params': [self._rotation], 'lr': training_args.rotation_lr, "name": "rotation"},
            {'params': [self._coefs], 'lr': training_args.deformation_lr_init * self.spatial_lr_scale, "name": "coefs"}
        ]

        self.optimizer = torch.optim.Adam(l, lr=0.0, eps=1e-15)
        
        self.xyz_scheduler_args = get_expon_lr_func(lr_init=training_args.position_lr_init*self.spatial_lr_scale,
                                                    lr_final=training_args.position_lr_final*self.spatial_lr_scale,
                                                    lr_delay_mult=training_args.position_lr_delay_mult,
                                                    max_steps=training_args.position_lr_max_steps)
        self.deformation_scheduler_args = get_expon_lr_func(lr_init=training_args.deformation_lr_init*self.spatial_lr_scale,
                                                    lr_final=training_args.deformation_lr_final*self.spatial_lr_scale,
                                                    lr_delay_mult=training_args.deformation_lr_delay_mult,
                                                    max_steps=training_args.position_lr_max_steps) 



    def update_learning_rate(self, iteration):
        ''' Learning rate scheduling per step '''
        for param_group in self.optimizer.param_groups:
            if param_group["name"] == "xyz":
                lr = self.xyz_scheduler_args(iteration)
                param_group['lr'] = lr
                # return lr
            if param_group["name"] == "coefs":
                lr = self.deformation_scheduler_args(iteration)
                param_group['lr'] = lr
            elif param_group["name"] == "deformation":
                lr = self.deformation_scheduler_args(iteration)
                param_group['lr'] = lr
                # return lr

    def construct_list_of_attributes(self):
        l = ['x', 'y', 'z', 'nx', 'ny', 'nz']
        # All channels except the 3 DC
        for i in range(self._features_dc.shape[1]*self._features_dc.shape[2]):
            l.append('f_dc_{}'.format(i))
        for i in range(self._features_rest.shape[1]*self._features_rest.shape[2]):
            l.append('f_rest_{}'.format(i))
        l.append('opacity')
        for i in range(self._scaling.shape[1]):
            l.append('scale_{}'.format(i))
        for i in range(self._rotation.shape[1]):
            l.append('rot_{}'.format(i))
        for i in range(self._coefs.shape[1]):
            l.append('coefs_{}'.format(i))
        return l

    def load_model(self, path):
        print("loading model from {}".format(path))
    
    # Load deformation data if exists (from old code)
        self._deformation_table = torch.gt(torch.ones((self.get_xyz.shape[0]), device="cuda"), 0)
        if os.path.exists(os.path.join(path, "deformation_table.pth")):
            self._deformation_table = torch.load(os.path.join(path, "deformation_table.pth"), map_location="cuda")
        
        self._deformation_accum = torch.zeros((self.get_xyz.shape[0], 3), device="cuda")
        if os.path.exists(os.path.join(path, "deformation_accum.pth")):
            self._deformation_accum = torch.load(os.path.join(path, "deformation_accum.pth"), map_location="cuda")

        # Load main model data (from new code)
        state_dict = torch.load(path)
        new_xyz = state_dict['xyz']
        new_features_dc = state_dict['feature_dc']
        new_features_rest = state_dict['feature_rest']
        new_opacity = state_dict['opacity']
        new_scaling = state_dict['scaling']
        new_rotation = state_dict['rotation']
        new_coefs = state_dict['coef']
        new_unique_kfIDs = state_dict['unique_kfIDs']
        new_n_obs = state_dict['n_obs']
    
        self.start_time = state_dict['start_time']
        self.max_time = state_dict['max_time']
    
        self.densification_postfix(
            new_xyz,
            new_features_dc,
            new_features_rest,
            new_opacity,
            new_scaling,
            new_rotation,
            new_coefs=new_coefs,
            new_kf_ids=new_unique_kfIDs,
            new_n_obs=new_n_obs,
        )

    def save_model(self, path):
        mkdir_p(os.path.dirname(path))
        state_dict = {
            'xyz': self._xyz,
            'feature_dc': self._features_dc,
            'feature_rest': self._features_rest,
            'opacity': self._opacity,
            'scaling': self._scaling,
            'rotation': self._rotation,
            'coef': self._coefs,
            'unique_kfIDs': self.unique_kfIDs,
            'n_obs': self.n_obs,
            'deform': self.deform,
            'start_time': self.start_time,
            'time': self.time,
            'max_time': self.max_time
        }
        torch.save(state_dict, path)
    
        # Save deformation data (from old code)
        if hasattr(self, '_deformation_table'):
            torch.save(self._deformation_table, os.path.join(path, "deformation_table.pth"))
        if hasattr(self, '_deformation_accum'):
            torch.save(self._deformation_accum, os.path.join(path, "deformation_accum.pth"))
            
    def load_ply(self, path):
        plydata = PlyData.read(path)
    
        # Add point cloud loading from new code
        def fetchPly_nocolor(path):
            plydata = PlyData.read(path)
            vertices = plydata["vertex"]
            positions = np.vstack([vertices["x"], vertices["y"], vertices["z"]]).T
            normals = np.vstack([vertices["nx"], vertices["ny"], vertices["nz"]]).T
            colors = np.ones_like(positions)
            return BasicPointCloud(points=positions, colors=colors, normals=normals)

        self.ply_input = fetchPly_nocolor(path)

        xyz = np.stack((
            np.asarray(plydata.elements[0]["x"]),
            np.asarray(plydata.elements[0]["y"]),
            np.asarray(plydata.elements[0]["z"])
        ), axis=1)
        opacities = np.asarray(plydata.elements[0]["opacity"])[..., np.newaxis]

        features_dc = np.zeros((xyz.shape[0], 3, 1))
        features_dc[:, 0, 0] = np.asarray(plydata.elements[0]["f_dc_0"])
        features_dc[:, 1, 0] = np.asarray(plydata.elements[0]["f_dc_1"])
        features_dc[:, 2, 0] = np.asarray(plydata.elements[0]["f_dc_2"])

        extra_f_names = [p.name for p in plydata.elements[0].properties if p.name.startswith("f_rest_")]
        extra_f_names = sorted(extra_f_names, key=lambda x: int(x.split("_")[-1]))
        assert len(extra_f_names) == 3 * (self.max_sh_degree + 1) ** 2 - 3
        features_extra = np.zeros((xyz.shape[0], len(extra_f_names)))
        for idx, attr_name in enumerate(extra_f_names):
            features_extra[:, idx] = np.asarray(plydata.elements[0][attr_name])
        features_extra = features_extra.reshape((features_extra.shape[0], 3, (self.max_sh_degree + 1) ** 2 - 1))

        scale_names = [p.name for p in plydata.elements[0].properties if p.name.startswith("scale_")]
        scale_names = sorted(scale_names, key=lambda x: int(x.split("_")[-1]))
        scales = np.zeros((xyz.shape[0], len(scale_names)))
        for idx, attr_name in enumerate(scale_names):
            scales[:, idx] = np.asarray(plydata.elements[0][attr_name])

        rot_names = [p.name for p in plydata.elements[0].properties if p.name.startswith("rot")]
        rot_names = sorted(rot_names, key=lambda x: int(x.split("_")[-1]))
        rots = np.zeros((xyz.shape[0], len(rot_names)))
        for idx, attr_name in enumerate(rot_names):
            rots[:, idx] = np.asarray(plydata.elements[0][attr_name])

        coef_names = [p.name for p in plydata.elements[0].properties if p.name.startswith("coefs_")]
        coef_names = sorted(coef_names, key=lambda x: int(x.split('_')[-1]))
        coefs = np.zeros((xyz.shape[0], len(coef_names)))
        for idx, attr_name in enumerate(coef_names):
            coefs[:, idx] = np.asarray(plydata.elements[0][attr_name])

        # Convert to tensors and set up parameters
        new_xyz = torch.tensor(xyz, dtype=torch.float, device="cuda")
        new_features_dc = nn.Parameter(
            torch.tensor(features_dc, dtype=torch.float, device="cuda")
            .transpose(1, 2)
            .contiguous()
            .requires_grad_(True)
        )
        new_features_rest = nn.Parameter(
            torch.tensor(features_extra, dtype=torch.float, device="cuda")
            .transpose(1, 2)
            .contiguous()
            .requires_grad_(True)
        )
        new_opacity = nn.Parameter(
            torch.tensor(opacities, dtype=torch.float, device="cuda").requires_grad_(True)
        )
        new_scaling = nn.Parameter(
            torch.tensor(scales, dtype=torch.float, device="cuda").requires_grad_(True)
        )
        new_rotation = nn.Parameter(
            torch.tensor(rots, dtype=torch.float, device="cuda").requires_grad_(True)
        )
        new_coefs = nn.Parameter(
            torch.tensor(coefs, dtype=torch.float, device="cuda").requires_grad_(True)
        )
    
        self.max_radii2D = torch.zeros((new_xyz.shape[0]), device="cuda")
        new_unique_kfIDs = torch.zeros((new_xyz.shape[0]))
        new_n_obs = torch.zeros((new_xyz.shape[0]), device="cpu").int()
    
        self.densification_postfix(
            new_xyz,
            new_features_dc,
            new_features_rest,
            new_opacity,
            new_scaling,
            new_rotation,
            new_coefs=new_coefs,
            new_kf_ids=new_unique_kfIDs,
            new_n_obs=new_n_obs,
        )

    def save_ply(self, path):
        mkdir_p(os.path.dirname(path))

        xyz = self._xyz.detach().cpu().numpy()
        normals = np.zeros_like(xyz)
        f_dc = (
            self._features_dc.detach()
            .transpose(1, 2)
            .flatten(start_dim=1)
            .contiguous()
            .cpu()
            .numpy()
        )
        f_rest = (
            self._features_rest.detach()
            .transpose(1, 2)
            .flatten(start_dim=1)
            .contiguous()
            .cpu()
            .numpy()
        )
        opacities = self._opacity.detach().cpu().numpy()
        scale = self._scaling.detach().cpu().numpy()
        rotation = self._rotation.detach().cpu().numpy()
        coefs = self._coefs.detach().cpu().numpy()

        dtype_full = [
            (attribute, "f4") for attribute in self.construct_list_of_attributes()
        ]
        elements = np.empty(xyz.shape[0], dtype=dtype_full)
        attributes = np.concatenate(
            (xyz, normals, f_dc, f_rest, opacities, scale, rotation, coefs), axis=1
        )
        elements[:] = list(map(tuple, attributes))
        el = PlyElement.describe(elements, "vertex")
        PlyData([el]).write(path)


    def reset_opacity(self):
        opacities_new = inverse_sigmoid(torch.min(self.get_opacity, torch.ones_like(self.get_opacity)*0.01))
        optimizable_tensors = self.replace_tensor_to_optimizer(opacities_new, "opacity")
        self._opacity = optimizable_tensors["opacity"]

    def replace_tensor_to_optimizer(self, tensor, name):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            if group["name"] == name:
                stored_state = self.optimizer.state.get(group['params'][0], None)
                stored_state["exp_avg"] = torch.zeros_like(tensor)
                stored_state["exp_avg_sq"] = torch.zeros_like(tensor)

                del self.optimizer.state[group['params'][0]]
                group["params"][0] = nn.Parameter(tensor.requires_grad_(True))
                self.optimizer.state[group['params'][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
        return optimizable_tensors

    def _prune_optimizer(self, mask):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            if len(group["params"]) > 1:
                continue
            stored_state = self.optimizer.state.get(group['params'][0], None)
            if stored_state is not None:
                stored_state["exp_avg"] = stored_state["exp_avg"][mask]
                stored_state["exp_avg_sq"] = stored_state["exp_avg_sq"][mask]
                del self.optimizer.state[group['params'][0]]
                group["params"][0] = nn.Parameter((group["params"][0][mask].requires_grad_(True)))
                self.optimizer.state[group['params'][0]] = stored_state
                optimizable_tensors[group["name"]] = group["params"][0]
            else:
                group["params"][0] = nn.Parameter(group["params"][0][mask].requires_grad_(True))
                optimizable_tensors[group["name"]] = group["params"][0]
        return optimizable_tensors

    def prune_points(self, mask):
        valid_points_mask = ~mask
        optimizable_tensors = self._prune_optimizer(valid_points_mask)

        self._xyz = optimizable_tensors["xyz"]
        self._features_dc = optimizable_tensors["f_dc"]
        self._features_rest = optimizable_tensors["f_rest"]
        self._opacity = optimizable_tensors["opacity"]
        self._scaling = optimizable_tensors["scaling"]
        self._rotation = optimizable_tensors["rotation"]
        self._coefs = optimizable_tensors["coefs"]
        self._deformation_accum = self._deformation_accum[valid_points_mask]
        self.xyz_gradient_accum = self.xyz_gradient_accum[valid_points_mask]
        self._deformation_table = self._deformation_table[valid_points_mask]
        self.denom = self.denom[valid_points_mask]
        self.max_radii2D = self.max_radii2D[valid_points_mask]

    def cat_tensors_to_optimizer(self, tensors_dict):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            if len(group["params"])>1 or group["name"]=='deformation':
                continue
            assert len(group["params"]) == 1
            extension_tensor = tensors_dict[group["name"]]
            stored_state = self.optimizer.state.get(group['params'][0], None)
            if stored_state is not None:
                stored_state["exp_avg"] = torch.cat((stored_state["exp_avg"], torch.zeros_like(extension_tensor)), dim=0)
                stored_state["exp_avg_sq"] = torch.cat((stored_state["exp_avg_sq"], torch.zeros_like(extension_tensor)), dim=0)

                del self.optimizer.state[group['params'][0]]
                group["params"][0] = nn.Parameter(torch.cat((group["params"][0], extension_tensor), dim=0).requires_grad_(True))
                self.optimizer.state[group['params'][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
            else:
                group["params"][0] = nn.Parameter(torch.cat((group["params"][0], extension_tensor), dim=0).requires_grad_(True))
                optimizable_tensors[group["name"]] = group["params"][0]

        return optimizable_tensors

    def densification_postfix(self, new_xyz, new_features_dc, new_features_rest, new_opacities, new_scaling, new_rotation, new_coefs, new_deformation_table):
        d = {"xyz": new_xyz,
        "f_dc": new_features_dc,
        "f_rest": new_features_rest,
        "opacity": new_opacities,
        "scaling" : new_scaling,
        "rotation" : new_rotation,
        "coefs": new_coefs
       }

        optimizable_tensors = self.cat_tensors_to_optimizer(d)
        self._xyz = optimizable_tensors["xyz"]
        self._features_dc = optimizable_tensors["f_dc"]
        self._features_rest = optimizable_tensors["f_rest"]
        self._opacity = optimizable_tensors["opacity"]
        self._scaling = optimizable_tensors["scaling"]
        self._rotation = optimizable_tensors["rotation"]
        self._coefs = optimizable_tensors["coefs"]
        
        self._deformation_table = torch.cat([self._deformation_table,new_deformation_table],-1)
        self.xyz_gradient_accum = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self._deformation_accum = torch.zeros((self.get_xyz.shape[0], 3), device="cuda")
        self.denom = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.max_radii2D = torch.zeros((self.get_xyz.shape[0]), device="cuda")

    def densify_and_split(self, grads, grad_threshold, scene_extent, N=2):
        n_init_points = self.get_xyz.shape[0]
        # Extract points that satisfy the gradient condition
        padded_grad = torch.zeros((n_init_points), device="cuda")
        padded_grad[:grads.shape[0]] = grads.squeeze()
        selected_pts_mask = torch.where(padded_grad >= grad_threshold, True, False)
        selected_pts_mask = torch.logical_and(selected_pts_mask,
                                              torch.max(self.get_scaling, dim=1).values > self.percent_dense*scene_extent)
        if not selected_pts_mask.any():
            return
        stds = self.get_scaling[selected_pts_mask].repeat(N,1)
        means = torch.zeros((stds.size(0), 3),device="cuda")
        samples = torch.normal(mean=means, std=stds)
        rots = build_rotation(self._rotation[selected_pts_mask]).repeat(N,1,1)
        new_xyz = torch.bmm(rots, samples.unsqueeze(-1)).squeeze(-1) + self.get_xyz[selected_pts_mask].repeat(N, 1)
        new_scaling = self.scaling_inverse_activation(self.get_scaling[selected_pts_mask].repeat(N,1) / (0.8*N))
        new_rotation = self._rotation[selected_pts_mask].repeat(N,1)
        new_features_dc = self._features_dc[selected_pts_mask].repeat(N,1,1)
        new_features_rest = self._features_rest[selected_pts_mask].repeat(N,1,1)
        new_opacity = self._opacity[selected_pts_mask].repeat(N,1)
        new_coefs = self._coefs[selected_pts_mask].repeat(N,1)
        new_deformation_table = self._deformation_table[selected_pts_mask].repeat(N)
        self.densification_postfix(new_xyz, new_features_dc, new_features_rest, new_opacity, new_scaling, new_rotation, new_coefs,new_deformation_table)

        prune_filter = torch.cat((selected_pts_mask, torch.zeros(N * selected_pts_mask.sum(), device="cuda", dtype=bool)))
        self.prune_points(prune_filter)

    def densify_and_clone(self, grads, grad_threshold, scene_extent):
        # Extract points that satisfy the gradient condition
        selected_pts_mask = torch.where(torch.norm(grads, dim=-1) >= grad_threshold, True, False)
        selected_pts_mask = torch.logical_and(selected_pts_mask,
                                              torch.max(self.get_scaling, dim=1).values <= self.percent_dense*scene_extent)
        
        new_xyz = self._xyz[selected_pts_mask] 
        new_features_dc = self._features_dc[selected_pts_mask]
        new_features_rest = self._features_rest[selected_pts_mask]
        new_opacities = self._opacity[selected_pts_mask]
        new_scaling = self._scaling[selected_pts_mask]
        new_rotation = self._rotation[selected_pts_mask]
        new_coefs    = self._coefs[selected_pts_mask]
        new_deformation_table = self._deformation_table[selected_pts_mask]

        self.densification_postfix(new_xyz, new_features_dc, new_features_rest, new_opacities, new_scaling, new_rotation,new_coefs, new_deformation_table)
    
    def prune(self, max_grad, min_opacity, extent, max_screen_size):
        prune_mask = (self.get_opacity < min_opacity).squeeze()
        if max_screen_size:
            big_points_vs = self.max_radii2D > max_screen_size
            # big_points_ws = self.get_scaling.max(dim=1).values > 0.1 * extent
            prune_mask = torch.logical_or(prune_mask, big_points_vs)
            # prune_mask = torch.logical_or(torch.logical_or(prune_mask, big_points_vs), big_points_ws)
        self.prune_points(prune_mask)
        torch.cuda.empty_cache()

    def densify(self, max_grad, min_opacity, extent, max_screen_size):
        grads = self.xyz_gradient_accum / self.denom
        grads[grads.isnan()] = 0.0

        self.densify_and_clone(grads, max_grad, extent)
        self.densify_and_split(grads, max_grad, extent)
    
    def standard_constaint(self):
        means3D = self._xyz.detach()
        scales = self._scaling.detach()
        rotations = self._rotation.detach()
        opacity = self._opacity.detach()
        time =  torch.tensor(0).to("cuda").repeat(means3D.shape[0],1)
        means3D_deform, scales_deform, rotations_deform, _ = self._deformation(means3D, scales, rotations, opacity, time)
        position_error = (means3D_deform - means3D)**2
        rotation_error = (rotations_deform - rotations)**2 
        scaling_erorr = (scales_deform - scales)**2
        return position_error.mean() + rotation_error.mean() + scaling_erorr.mean()


    def add_densification_stats(self, viewspace_point_tensor, update_filter):
        self.xyz_gradient_accum[update_filter] += torch.norm(viewspace_point_tensor[update_filter,:2], dim=-1, keepdim=True)
        self.denom[update_filter] += 1

    @torch.no_grad()
    def update_deformation_table(self,threshold):
        # print("origin deformation point nums:",self._deformation_table.sum())
        self._deformation_table = torch.gt(self._deformation_accum.max(dim=-1).values/100,threshold)


    def piece_wise_gaussian_deformation(self, t):
        idx = torch.tensor(t*CURVE_NUM).floor().int().item()
        
        min_idx = max(idx - GAUSSIAN_NUM//2, 0)
        max_idx = min(idx + GAUSSIAN_NUM//2, MAX_NUM)
        
        N = len(self._xyz)
        coefs = self._coefs.reshape(N, CH_NUM, 3 , MAX_NUM).contiguous() 
        # piece_wise_coefs = torch.split(coefs, [min_idx, max_idx-min_idx, MAX_NUM-max_idx], -1)[1]
        piece_wise_coefs = coefs[..., min_idx:max_idx]
        weight, mu, sigma = torch.chunk(piece_wise_coefs,3,-2)                         # [N, C:11, 1, m:10]
        # mu = torch.arange(min_idx, max_idx, dtype=weight.dtype, device=weight.device)*INTERVAL
        exponent = (t - mu)**2/(sigma**2+1e-6)
        gaussian =  torch.exp(-exponent**2)         
        return (gaussian*weight).sum(-1).squeeze()


    def partial_gaussian_deformation(self, t, ch_num=10, basis_num=17):
        """
        Applies a piecewise linear combination of learnable Gaussian basis functions to model surface deformation,
        reducing the number of active basis functions to accelerate training.

        Args:
        t (torch.Tensor): The input time step.
        ch_num (int): The number of channels in the deformation tensor (default: 10 = 3 (pos) + 3 (scale) + 4 (rot)).
        basis_num (int): The total number of Gaussian basis functions.

        Returns:
            torch.Tensor: The deformed model tensor.
        """
        idx = torch.tensor(t * basis_num).floor().int().item() + 2
        min_idx = max(idx - self.gm_num, 0) if self.gm_num > 0 else 0
        max_idx = min(idx + self.gm_num, basis_num)
        
        N = len(self._xyz)
        coefs = self._coefs.reshape(N, ch_num, 3, basis_num).contiguous()
        coefs = torch.split(coefs, [min_idx, max_idx - min_idx, basis_num - max_idx], -1)
        
        deform = 0
        for i, coef in enumerate(coefs):
            if i != 1:
                coef = coef.clone().detach()
            weight, mu, sigma = torch.chunk(coef, 3, -2)
            exponent = (t - mu) ** 2 / (sigma ** 2 + 1e-6)
            gaussian = torch.exp(-exponent ** 2)
            deform += (gaussian * weight).sum(-1).squeeze()
        
        return deform

    def deformation(self, xyz: torch.Tensor, scales: torch.Tensor, rotations: torch.Tensor, time: float) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        Apply flexible deformation modeling to the Gaussian model with reduced learnable parameters by selecting only active basis functions.

        Args:
            xyz (torch.Tensor): The current positions of the model vertices. (shape: [N, 3])
            scales (torch.Tensor): The current scales per Gaussian primitive. (shape: [N, 3])
            rotations (torch.Tensor): The current rotations of the model. (shape: [N, 4])
            time (float): The current time.

        Returns:
            tuple: A tuple containing the updated positions, scaling factors, and rotations of the model.
                   (xyz: torch.Tensor, scales: torch.Tensor, rotations: torch.Tensor)
        """
        if self.start_time is None:
            self.start_time = time
        self.time = time
        self.deform = self.partial_gaussian_deformation((time-self.start_time)/self.max_time)
        self.get_deformation()
        
        deform = self.partial_gaussian_deformation(time, ch_num=self.args.ch_num, basis_num=self.args.curve_num)
        
        deform_xyz = deform[:, :3]
        xyz += deform_xyz
        deform_rot = deform[:, 3:7]
        rotations += deform_rot
        try:
            deform_scaling = deform[:, 7:10]
            scales += deform_scaling
            return xyz, scales, rotations
        except:
            return xyz, scales, rotations

    
    def gaussian_deformation(self, t):
        N = len(self._xyz)
        coefs = self._coefs.reshape(N, CH_NUM, 3 , CURVE_NUM).contiguous() 
        weight, mu, sigma = torch.chunk(coefs,3,-2)                         # [N, C:11, 1, m:10]
        exponent = (t - mu)**2/(sigma**2+1e-6)
        gaussian =  torch.exp(-exponent**2)         
        return (gaussian*weight).sum(-1).squeeze()

        
    def get_deformation(self):
        # deform = self.piece_wise_gaussian_deformation(time-self.start_time)
        self.deform_xyz = self.deform[:,:3]
        self.deform_rot = self.deform[:, 3:7]
        self.deform_scaling = self.deform[:, 7:10]
        if CH_NUM>=11:
            # self.deform_opacity = deform[:, 10:]
            self.deform_brightness = self.deform[:, None, 10:]

    def print_deformation_weight_grad(self):
        for name, weight in self._deformation.named_parameters():
            if weight.requires_grad:
                if weight.grad is None:
                    
                    print(name," :",weight.grad)
                else:
                    if weight.grad.mean() != 0:
                        print(name," :",weight.grad.mean(), weight.grad.min(), weight.grad.max())
        print("-"*50)
    
    def compute_sparsity_regulation(self,):
        N = len(self._xyz)
        ch_num = self.args.ch_num
        coefs = self._coefs.reshape(N, ch_num, -1).contiguous() # [N, 7, ORDER_NUM + ORDER_NUM * 2 ]
        return (torch.sum(torch.abs(coefs), dim=-1, keepdim=True)\
            /torch.abs(coefs.max(dim=-1, keepdim = True)[0])).mean()   
        
    def compute_l1_regulation(self,):

        return (torch.abs(self._coefs)).mean()
    
    def compute_l2_regulation(self,):

        return (self._coefs**2).mean()
    
    def _plane_regulation(self):
        multi_res_grids = self._deformation.deformation_net.grid.grids
        total = 0
        # model.grids is 6 x [1, rank * F_dim, reso, reso]
        for grids in multi_res_grids:
            if len(grids) == 3:
                time_grids = []
            else:
                time_grids =  [0,1,3]
            for grid_id in time_grids:
                total += compute_plane_smoothness(grids[grid_id])
        return total

    def _time_regulation(self):
        multi_res_grids = self._deformation.deformation_net.grid.grids
        total = 0
        # model.grids is 6 x [1, rank * F_dim, reso, reso]
        for grids in multi_res_grids:
            if len(grids) == 3:
                time_grids = []
            else:
                time_grids =[2, 4, 5]
            for grid_id in time_grids:
                total += compute_plane_smoothness(grids[grid_id])
        return total

    def _l1_regulation(self):
                # model.grids is 6 x [1, rank * F_dim, reso, reso]
        multi_res_grids = self._deformation.deformation_net.grid.grids

        total = 0.0
        for grids in multi_res_grids:
            if len(grids) == 3:
                continue
            else:
                # These are the spatiotemporal grids
                spatiotemporal_grids = [2, 4, 5]
            for grid_id in spatiotemporal_grids:
                total += torch.abs(1 - grids[grid_id]).mean()
        return total

    def compute_regulation(self, time_smoothness_weight, l1_time_planes_weight, plane_tv_weight):
        return plane_tv_weight * self._plane_regulation() + time_smoothness_weight * self._time_regulation() + l1_time_planes_weight * self._l1_regulation()

    def clear_deformation(self):
        self.deform = None
        self.deform_xyz = None
        self.deform_rot = None
        self.deform_scaling = None
        self.deform_opacity = None 
        self.deform_brightness = None

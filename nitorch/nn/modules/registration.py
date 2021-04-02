import torch
from torch import nn as tnn
from nitorch import core, spatial
from nitorch.core import py, utils
from .cnn import UNet2
from .base import Module
from .spatial import GridPull, GridResize, GridExp, GridShoot
from .. import check


class VoxelMorph(Module):
    """VoxelMorph warps a source/moving image to a fixed/target image.

    A VoxelMorph network is obtained by concatenating a UNet and a
    (diffeomorphic) spatial transformer. The loss is made of two terms:
    an image similarity loss and a velocity regularisation loss.

    The original U-Net structure used by VoxelMorph is described in [2].
    It works at 5 different resolutions, with the number of features at 
    each encoding scale being [16, 32, 32, 32, 32]. The first number 
    corresponds to feature extraction at the initial resolution, and 
    the last number is the number of output features at the coarsest 
    resolution (the bottleneck). In the decoder, the number of features
    at each scale is [32, 32, 32, 32], each of these feature map is 
    concatenated with the output from the decoder at the same scale.
    Finally, two convolutions with 16 output features are applied 
    (without change of scale) followed by a final convolution with 
    3 output features (the three components of the displacement or
    velocity field). Note that all encoding and decoding convolutions
    have kernel size 3 and stride 2 -- therefore no max-pooling or
    linear upsampling is used. All convolutions are followed by a 
    leaky ReLU activation sith slope 0.2. The default parameters of 
    out implementation follow this architecture.
    
    Note that a slighlty different architecture was proposed in [1], 
    where two convolutions were applied at the second-to-last scale.
    This module does not implement this architecture. However, 
    our U-Net is highly parameterised, and alternative pooling and 
    upsampling methods, activation functions and number of convolutions
    per scale can be used.
    
    A scaling and squaring layer is used to integrate the output 
    velocity field and generate a diffeomorphic transformation, 
    as in [3, 4]. If the number of integration steps is set at 0, 
    a small deformation model (without integration) is used. 
    Alternatively, a novel geodesic shooting layer can be used 
    by setting `shoot=True` in the exponentiation structure.

    References
    ----------
    .. [1] "An Unsupervised Learning Model for Deformable Medical Image Registration"
        Guha Balakrishnan, Amy Zhao, Mert R. Sabuncu, John Guttag, Adrian V. Dalca
        CVPR 2018. eprint arXiv:1802.02604
    .. [2] "VoxelMorph: A Learning Framework for Deformable Medical Image Registration"
        Guha Balakrishnan, Amy Zhao, Mert R. Sabuncu, John Guttag, Adrian V. Dalca
        IEEE TMI 2019. eprint arXiv:1809.05231
    .. [3] "Unsupervised Learning for Fast Probabilistic Diffeomorphic Registration"
        Adrian V. Dalca, Guha Balakrishnan, John Guttag, Mert R. Sabuncu
        MICCAI 2018. eprint arXiv:1805.04605
    .. [4] "Unsupervised Learning of Probabilistic Diffeomorphic Registration for Images and Surfaces"
        Adrian V. Dalca, Guha Balakrishnan, John Guttag, Mert R. Sabuncu
        MedIA 2019. eprint arXiv:1903.03545
    """

    def __init__(self, dim, unet=None, pull=None, exp=None,
                 *, in_channels=2):
        """

        Parameters
        ----------
        dim : int
            Dimensionality of the input (1|2|3)
        unet : dict
            Dictionary of U-Net parameters with fields:
                encoder : sequence[int], default=[16, 32, 32, 32, 32]
                decoder : sequence[int], default=[32, 32, 32, 32, 16, 16]
                conv_per_layer : int, default=1
                kernel_size : int, default=3
                activation : str or callable, default=LeakyReLU(0.2)
                pool : {'max', 'conv', 'down', None}, default=None
                    'max'  -> 2x2x2 max-pooling
                    'conv' -> 2x2x2 strided convolution (no bias, no activation)
                    'down' -> downsampling
                     None  -> use strided convolutions in the encoder
                unpool : {'conv', 'up', None}, default=None
                    'conv' -> 2x2x2 strided convolution (no bias, no activation)
                    'up'   -> linear upsampling
                     None  -> use strided convolutions in the decoder
        pull : dict
            Dictionary of Transformer parameters with fields:
                interpolation : {0..7}, default=1
                bound : str, default='dct2'
                extrapolate : bool, default=False
        exp : dict
            Dictionary of Exponentiation parameters with fields:
                interpolation : {0..7}, default=1
                bound : str, default='dft'
                steps : int, default=8
                shoot : bool, default=False
                downsample : float, default=2
            If shoot is True, these fields are also present:
                absolute : float, default=0.0001
                membrane : float, default=0.001
                bending : float, default=0.2
                lame : (float, float), default=(0.05, 0.2)
        """
        # default parameters
        unet = dict(unet or {})
        unet.setdefault('encoder', [16, 32, 32, 32, 32])
        unet.setdefault('decoder', [32, 32, 32, 32, 16, 16])
        unet.setdefault('kernel_size', 3)
        unet.setdefault('pool', None)
        unet.setdefault('unpool', None)
        unet.setdefault('activation', tnn.LeakyReLU(0.2))
        pull = dict(pull or {})
        pull.setdefault('interpolation', 1)
        pull.setdefault('bound', 'dct2')
        pull.setdefault('extrapolate', False)
        exp = dict(exp or {})
        exp.setdefault('interpolation', 1)
        exp.setdefault('bound', 'dft')
        exp.setdefault('steps', 8)
        exp.setdefault('shoot', False)
        exp.setdefault('downsample', 2)
        exp.setdefault('absolute', 0.0001)
        exp.setdefault('membrane', 0.001)
        exp.setdefault('bending', 0.2)
        exp.setdefault('lame', (0.05, 0.2))
        exp.setdefault('factor', 1)
        do_shoot = exp.pop('shoot')
        downsample_vel = utils.make_vector(exp.pop('downsample'), dim).tolist()
        vel_inter = exp['interpolation']
        vel_bound = exp['bound']
        if do_shoot:
            exp.pop('interpolation')
            exp.pop('bound')
            exp.pop('voxel_size', downsample_vel)
            exp['factor'] *= py.prod(downsample_vel)
        else:
            exp.pop('absolute')
            exp.pop('membrane')
            exp.pop('bending')
            exp.pop('lame')
            exp.pop('factor')

        # prepare layers
        super().__init__()
        self.unet = UNet2(dim, in_channels, dim, **unet,)
        self.resize = GridResize(interpolation=vel_inter, bound=vel_bound,
                                 factor=[1/f for f in downsample_vel])
        self.velexp = GridShoot(**exp) if do_shoot else GridExp(**exp)
        self.pull = GridPull(**pull)
        self.dim = dim

        # register losses/metrics
        self.tags = ['image', 'velocity', 'segmentation']

    def exp(self, velocity, displacement=False):
        """Generate a deformation grid from tangent parameters.

        Parameters
        ----------
        velocity : (batch, *spatial, nb_dim)
            Stationary velocity field
        displacement : bool, default=False
            Return a displacement field (voxel to shift) rather than
            a transformation field (voxel to voxel).

        Returns
        -------
        grid : (batch, *spatial, nb_dim)
            Deformation grid (transformation or displacement).

        """
        # generate grid
        shape = velocity.shape[1:-1]
        velocity_small = self.resize(velocity, type='displacement')
        grid = self.velexp(velocity_small, displacement=displacement)
        grid = self.resize(grid, shape=shape, factor=None,
                           type='disp' if displacement else 'grid')
        return grid

    def forward(self, source, target, source_seg=None, target_seg=None,
                *, _loss=None, _metric=None):
        """

        Parameters
        ----------
        source : tensor (batch, channel, *spatial)
            Source/moving image
        target : tensor (batch, channel, *spatial)
            Target/fixed image
        source_seg : tensor (batch, classes, *spatial), optional
            Source/moving segmentation
        target_seg : tensor (batch, classes, *spatial), optional
            Target/fixed segmentation

        Other Parameters
        ----------------
        _loss : dict, optional
            If provided, all registered losses are computed and appended.
        _metric : dict, optional
            If provided, all registered metrics are computed and appended.

        Returns
        -------
        deformed_source : tensor (batch, channel, *spatial)
            Deformed source image
        deformed_source_seg : tensor (batch, classes, *spatial), optional
            Deformed source segmentation
        velocity : tensor (batch,, *spatial, len(spatial))
            Velocity field

        """
        # sanity checks
        check.dim(self.dim, source, target)
        check.shape(target, source, dims=[0], broadcast_ok=True)
        check.shape(target, source, dims=range(2, self.dim+2))
        check.shape(target_seg, source_seg, dims=[0], broadcast_ok=True)
        check.shape(target_seg, source_seg, dims=range(2, self.dim+2))

        # chain operations
        source_and_target = torch.cat((source, target), dim=1)
        velocity = self.unet(source_and_target)
        velocity = core.utils.channel2last(velocity)
        grid = self.exp(velocity)
        deformed_source = self.pull(source, grid)

        if source_seg is not None:
            if source_seg.shape[2:] != source.shape[2:]:
                grid = spatial.resize_grid(grid, shape=source_seg.shape[2:])
            deformed_source_seg = self.pull(source_seg, grid)
        else:
            deformed_source_seg = None

        # compute loss and metrics
        self.compute(_loss, _metric,
                     image=[deformed_source, target],
                     velocity=[velocity],
                     segmentation=[deformed_source_seg, target_seg])

        if source_seg is None:
            return deformed_source, velocity
        else:
            return deformed_source, deformed_source_seg, velocity

    def board(self, tb, **k):
        """Tensorboard visualization function"""
        implicit = getattr(self, 'implicit', False)
        return registration_board(self, tb, **k, implicit=implicit)


def registration_board(
        self, tb,
        inputs=None, outputs=None, epoch=None, minibatch=None, mode=None,
        implicit=False, do_eval=True, do_train=True, **kwargs):
    """Plug-and-play tensorboard method for registration networks

    Parameters
    ----------
    self : Module
    tb : SummaryWriter
    inputs : tuple of tensor
        (source, target, [source_seg, target_seg])
    outputs : tuple of tensor
        (deformed_source, [deformed_source_seg], velocity)
    epoch : int
        Index of current epoch
    minibatch : int
        Index of current minibatch
    mode : {'train', 'eval'}
    implicit : bool, default=False
        Does the deformed segmentation have an implicit class?
    do_eval : bool, default=True
    do_train : bool, default=True
    kwargs : dict

    """
    if torch.is_grad_enabled():
        # run without gradients
        with torch.no_grad():
            return registration_board(self, tb, inputs, outputs, epoch,
                                      minibatch, mode, implicit, do_eval,
                                      do_train, **kwargs)

    if ((not do_eval and mode == 'eval') or
        (not do_train and mode == 'train') or
        inputs is None):
        return

    from nitorch.plot import get_orthogonal_slices, get_slice
    from nitorch.plot.colormaps import prob_to_rgb, intensity_to_rgb, disp_to_rgb
    import matplotlib.pyplot as plt

    def get_slice_seg(x):
        """Get slice + convert to probabilities (one-hot) if needed."""
        if x.dtype in (torch.float, torch.double):
            x = get_slice(x)
        else:
            x = get_slice(x[0])
            x = torch.stack([x == i for i in range(1, x.max().item() + 1)])
            x = x.float()
        return x

    def get_orthogonal_slices_seg(x):
        """Get slices + convert to probabilities (one-hot) if needed."""
        if x.dtype in (torch.float, torch.double):
            xs = get_orthogonal_slices(x)
        else:
            xs = get_orthogonal_slices(x[0])
            xs = [torch.stack([x == i for i in range(1, x.max().item() + 1)])
                  for x in xs]
            xs = [x.float() for x in xs]
        return xs

    source, target, *seg = inputs
    *warps, vel = outputs
    if seg:
        has_seg = True
        source_seg, target_seg = seg
        warped_source, warped_seg = warps
    else:
        has_seg = False
        warped_source, = warps
    del seg, warps, inputs, outputs
    is2d = source.dim() - 2 == 2

    fig = plt.figure()
    if is2d:  # 2D
        nrow = 3
        ncol = 1 + has_seg
        # images
        source = get_slice(source[0, 0])
        source = intensity_to_rgb(source)
        warped_source = get_slice(warped_source[0, 0])
        warped_source = intensity_to_rgb(warped_source)
        target = get_slice(target[0, 0])
        target = intensity_to_rgb(target)
        plt.subplot(nrow, ncol, 1)
        plt.imshow(source.detach().cpu())
        plt.axis('off')
        plt.subplot(nrow, ncol, 2)
        plt.imshow(warped_source.detach().cpu())
        plt.axis('off')
        plt.subplot(nrow, ncol, 3)
        plt.imshow(target.detach().cpu())
        plt.axis('off')
        # segmentations
        if has_seg:
            nk = warped_seg.shape[1] + implicit
            source_seg = get_slice(source_seg[0])
            source_seg = prob_to_rgb(source_seg, implicit=source_seg.shape[0] < nk)
            warped_seg = get_slice(warped_seg[0])
            warped_seg = prob_to_rgb(warped_seg, implicit=warped_seg.shape[0] < nk)
            target_seg = get_slice_seg(target_seg[0])
            target_seg = prob_to_rgb(target_seg, implicit=target_seg.shape[0] < nk)
            plt.subplot(nrow, ncol, 4)
            plt.imshow(source_seg.detach().cpu())
            plt.axis('off')
            plt.subplot(nrow, ncol, 5)
            plt.imshow(warped_seg.detach().cpu())
            plt.axis('off')
            plt.subplot(nrow, ncol, 6)
            plt.imshow(target_seg.detach().cpu())
            plt.axis('off')

    else:  # 3D
        nrow = 3
        ncol = 3*(1 + has_seg)
        # images
        source = get_orthogonal_slices(source[0, 0])
        source = [intensity_to_rgb(x) for x in source]
        warped_source = get_orthogonal_slices(warped_source[0, 0])
        warped_source = [intensity_to_rgb(x) for x in warped_source]
        target = get_orthogonal_slices(target[0, 0])
        target = [intensity_to_rgb(x) for x in target]
        for i in range(3):
            plt.subplot(nrow, ncol, 1 + i*ncol)
            plt.imshow(source[i].detach().cpu())
            plt.axis('off')
            plt.subplot(nrow, ncol, 2 + i*ncol)
            plt.imshow(warped_source[i].detach().cpu())
            plt.axis('off')
            plt.subplot(nrow, ncol, 3 + i*ncol)
            plt.imshow(target[i].detach().cpu())
            plt.axis('off')
        # segmentations
        if has_seg:
            nk = warped_seg.shape[1] + implicit
            source_seg = get_orthogonal_slices(source_seg[0])
            source_seg = [prob_to_rgb(x, implicit=x.shape[0] < nk) for x in source_seg]
            warped_seg = get_orthogonal_slices(warped_seg[0])
            warped_seg = [prob_to_rgb(x, implicit=x.shape[0] < nk) for x in warped_seg]
            target_seg = get_orthogonal_slices_seg(target_seg[0])
            target_seg = [prob_to_rgb(x, implicit=x.shape[0] < nk) for x in target_seg]
            for i in range(3):
                plt.subplot(nrow, ncol, 4 + i*ncol)
                plt.imshow(source_seg[i].detach().cpu())
                plt.axis('off')
                plt.subplot(nrow, ncol, 5 + i*ncol)
                plt.imshow(warped_seg[i].detach().cpu())
                plt.axis('off')
                plt.subplot(nrow, ncol, 6 + i*ncol)
                plt.imshow(target_seg[i].detach().cpu())
                plt.axis('off')
    plt.tight_layout()

    if not hasattr(self, 'tbstep'):
        self.tbstep = dict()
    self.tbstep.setdefault(mode, 0)
    self.tbstep[mode] += 1
    tb.add_figure(f'warps/{mode}', fig, global_step=self.tbstep[mode])

    fig = plt.figure()
    if is2d:
        vel = get_slice(utils.movedim(vel[0], -1, 0))
        vel = disp_to_rgb(vel, amplitude='saturation')
        plt.imshow(vel.detach().cpu())
        plt.axis('off')
    else:
        vel = get_orthogonal_slices(utils.movedim(vel[0], -1, 0))
#         vel = [disp_to_rgb(v, amplitude='saturation') for v in vel]
#         plt.subplot(1, 3, 1)
#         plt.imshow(vel[0].detach().cpu())
#         plt.axis('off')
#         plt.subplot(1, 3, 2)
#         plt.imshow(vel[1].detach().cpu())
#         plt.axis('off')
#         plt.subplot(1, 3, 3)
#         plt.imshow(vel[2].detach().cpu())
#         plt.axis('off')
        for i in range(3):
            for j in range(3):
                plt.subplot(3, 3, 1+j+i*3)
                plt.imshow(vel[j][i].detach().cpu())
                plt.colorbar()
                plt.axis('off')
    plt.tight_layout()

    tb.add_figure(f'vel/{mode}', fig, global_step=self.tbstep[mode])

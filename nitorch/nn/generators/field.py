import math
import torch
from nitorch.core.constants import pi
from nitorch.core import utils, py, linalg, fft
from nitorch import spatial
from nitorch.nn.base import Module
from .distribution import _get_dist


class RandomField(Module):
    """Sample a smooth random field.

    The field is generated by sampling white Gaussian noise and
    convolving it with a Gaussian kernel.

    """

    def __init__(self, shape=None, mean=0, amplitude=1, fwhm=1, channel=1,
                 basis=1, device=None, dtype=None):
        """

        Parameters
        ----------
        shape : sequence[int], optional
            Lattice shape
        mean : float or (channel,) vector_like, default=0
            Mean value.
        amplitude : float or (channel,) vector_like, default=1
            Amplitude of the squared-exponential kernel.
        fwhm : float or (channel,) vector_like, default=1
            Full-width at Half Maximum of the squared-exponential kernel.
        channel : int, default=1
            Number of channels
        basis : {0, 1}, default=1
            See `nitorch.core.kernels.smooth`
        device : torch.device, optional
            Output tensor device.
        dtype : torch.dtype, optional
            Output tensor datatype.

        """
        super().__init__()
        self.shape = shape
        self.mean = mean
        self.amplitude = amplitude
        self.fwhm = fwhm
        self.channel = channel
        self.basis = basis
        self.device = device
        if dtype is None or not dtype.is_floating_point:
            dtype = torch.get_default_dtype()
        self.dtype = dtype

    def to(self, *args, **kwargs):
        device, dtype, non_blocking, convert_to_format \
            = torch._C._nn._parse_to(*args, **kwargs)

        self.dtype = dtype or self.dtype
        self.device = device or self.device
        super().to(*args, **kwargs)

    def forward(self, batch=1, **overload):
        """

        Parameters
        ----------
        batch : int, default=1
            Batch size

        Other Parameters
        ----------------
        shape : sequence[int], optional
        channel : int, optional
        device : torch.device, optional
        dtype : torch.dtype, optional

        Returns
        -------
        field : (batch, channel, *shape) tensor
            Generated random field

        """

        # get arguments
        shape = overload.get('shape', self.shape)
        channel = overload.get('channel', self.channel)
        dtype = overload.get('dtype', self.dtype)
        device = overload.get('device', self.device)
        backend = dict(dtype=dtype, device=device)

        # sample if parameters are callable
        nb_dim = len(shape)

        # device/dtype
        mean = utils.make_vector(self.mean, channel, **backend)
        amplitude = utils.make_vector(self.amplitude, channel, **backend)
        fwhm = utils.make_vector(self.fwhm, channel, **backend)
        
        # convert SE parameters to noise/kernel parameters
        sigma_se = fwhm / math.sqrt(8*math.log(2))
        amplitude = amplitude * (2*pi)**(nb_dim/4) * sigma_se.sqrt()
        fwhm = fwhm * math.sqrt(2)
        
        # smooth
        out = torch.empty([batch, channel, *shape], **backend)
        for b in range(batch):
            for c in range(channel):
                sample = torch.distributions.Normal(mean[c], amplitude[c]).sample(shape)
                out[b, c] = spatial.smooth(
                    sample, 'gauss', fwhm,
                    basis=self.basis, bound='dct2', dim=nb_dim, padding='same')
        return out


class RandomFieldSpline(Module):
    """Sample a smooth random field.

    The field is generated by sampling b-spline coefficients
    from a Gaussian distribution.

    """

    def __init__(self, shape=None, mean=0, amplitude=1, fwhm=1, channel=1,
                 basis=3, device='cpu', dtype=None):
        """

        Parameters
        ----------
        shape : sequence[int], optional
            Lattice shape
        mean : float or (channel,) vector_like, default=0
            Mean value.
        amplitude : float or (channel,) vector_like, default=1
            Amplitude of the squared-exponential kernel.
        fwhm : float or (channel,) vector_like, default=1
            Full-width at Half Maximum of the squared-exponential kernel.
        channel : int, default=1
            Number of channels
        basis : {0..7}, default=None
            Use b-spline of this order.
        device : torch.device: default='cpu'
            Output tensor device.
        dtype : torch.dtype, default=torch.get_default_dtype()
            Output tensor datatype.

        """
        super().__init__()
        self.shape = shape
        self.mean = mean
        self.amplitude = amplitude
        self.fwhm = fwhm
        self.channel = channel
        self.basis = basis
        self.device = device
        if dtype is None or not dtype.is_floating_point:
            dtype = torch.get_default_dtype()
        self.dtype = dtype

    def forward(self, batch=1, **overload):
        """

        Parameters
        ----------
        batch : int, default=1
            Batch size

        Other Parameters
        ----------------
        shape : sequence[int], optional
        channel : int, optional
        device : torch.device, optional
        dtype : torch.dtype, optional

        Returns
        -------
        field : (batch, channel, *shape) tensor
            Generated random field

        """

        # get arguments
        shape = overload.get('shape', self.shape)
        channel = overload.get('channel', self.channel)
        dtype = overload.get('dtype', self.dtype)
        device = overload.get('device', self.device)
        backend = dict(dtype=dtype, device=device)

        # device/dtype
        nb_dim = len(shape)
        mean = utils.make_vector(self.mean, channel, **backend)
        amplitude = utils.make_vector(self.amplitude, channel, **backend)
        fwhm = utils.make_vector(self.fwhm, nb_dim, **backend)

        # sample spline coefficients
        nodes = [(s/f).ceil().int().item()
                 for s, f in zip(shape, fwhm)]
        sample = torch.randn([batch, channel, *nodes], **backend)
        sample *= utils.unsqueeze(amplitude, -1, nb_dim)
        sample = spatial.resize(sample, shape=shape, interpolation=self.basis,
                                bound='dct2', prefilter=False)
        sample += utils.unsqueeze(mean, -1, nb_dim)
        return sample


class RandomFieldGreens(Module):
    """Sample a Gaussian random field defined by its Greens function."""

    def __init__(self, shape=None, mean=0, channel=1,
                 absolute=1e-3, membrane=0.1, bending=0, voxel_size=1,
                 cache_greens=True, device='cpu', dtype=None):
        """

        Parameters
        ----------
        shape : sequence[int], optional
            Lattice shape
        mean : float or (channel,) vector_like, default=0
            Mean value.
        channel : int, default=1
            Number of channels
        absolute : float or (channel,) vector_like, default=1e-4
            Penalty on absolute displacements.
        membrane : float or (channel,) vector_like, default=0.1
            Penalty on membrane energy (first derivatives).
        bending : float or (channel,) vector_like, default=0
            Penalty on bending energy (second derivatives).
        voxel_size : float or (dim,) vector_like, default=1
            Voxel size of the lattice.
        cache_greens : bool, default=True
            Keep the last Greens kernel and only recompute if voxel_size
            changed.
        device : torch.device, optional
            Output tensor device.
        dtype : torch.dtype, optional
            Output tensor datatype.

        """
        super().__init__()
        self.shape = shape
        self.mean = mean
        self.channel = channel
        self.absolute = absolute
        self.membrane = membrane
        self.bending = bending
        self.voxel_size = voxel_size
        self.cache_greens = cache_greens
        self.device = device
        if dtype is None or not dtype.is_floating_point:
            dtype = torch.get_default_dtype()
        self.dtype = dtype

    def forward(self, batch=1, **overload):
        """

        Parameters
        ----------
        batch : int, default=1
            Batch size

        Other Parameters
        ----------------
        shape : sequence[int], optional
        channel : int, optional
        voxel_size : float or (dim,) vector_like, optional
        device : torch.device, optional
        dtype : torch.dtype, optional

        Returns
        -------
        field : (batch, channel, *shape) tensor
            Generated random field

        """

        # get arguments
        shape = overload.get('shape', self.shape)
        channel = overload.get('channel', self.channel)
        voxel_size = overload.get('voxel_size', self.voxel_size)
        dtype = overload.get('dtype', self.dtype)
        device = overload.get('device', self.device)
        backend = dict(dtype=dtype, device=device)

        # sample if parameters are callable
        nb_dim = len(shape)
        voxel_size = utils.make_vector(voxel_size, nb_dim, **backend)
        voxel_size = voxel_size.tolist()

        if (hasattr(self, '_greens')
                and self._voxel_size == voxel_size
                and self._channel == channel
                and self._shape == shape):
            greens = self._greens.to(dtype=dtype, device=device)
        else:
            mean = utils.make_vector(self.mean, channel, **backend)
            absolute = utils.make_vector(self.absolute, channel, **backend)
            membrane = utils.make_vector(self.membrane, channel, **backend)
            bending = utils.make_vector(self.bending, channel, **backend)

            greens = []
            for c in range(channel):
                greens.append(spatial.greens(
                    shape,
                    absolute=absolute[c],
                    membrane=membrane[c],
                    bending=bending[c],
                    lame=0,
                    voxel_size=voxel_size,
                    device=device,
                    dtype=dtype))
            greens = torch.stack(greens)
            greens = greens.sqrt_()

            if self.cache_greens:
                self._greens = greens
                self._voxel_size = voxel_size
                self._shape = shape

        # sample white noise
        sample = torch.randn([2, batch, channel, *shape], **backend)
        sample *= greens.unsqueeze(-1)
        sample = fft.complex(sample[0], sample[1])

        # inverse Fourier transform
        dims = list(range(-nb_dim, 0))
        sample = fft.real(fft.ifftn(sample, dim=dims))
        sample *= py.prod(shape)

        # add mean
        sample += utils.unsqueeze(mean, -1, len(shape))

        return sample


class RandomGridGreens(Module):
    """Sample a Gaussian random field defined by its Greens function."""

    def __init__(self, shape=None, mean=0,
                 absolute=1e-4, membrane=1e-3, bending=0.2, lame=(0.05, 0.2),
                 voxel_size=1, cache_greens=True, device='cpu', dtype=None):
        """

        Parameters
        ----------
        shape : sequence[int], optional
            Lattice shape
        mean : float, default=0
            Mean value.
        absolute : float, default=1e-4
            Penalty on absolute displacements.
        membrane : float, default=1e-3
            Penalty on membrane energy (first derivatives).
        bending : float, default=0.2
            Penalty on bending energy (second derivatives).
        lame : pair of float, default=(0.05, 0.2)
            Penalty on linear-elastic energy (zooms and shears).
        voxel_size : float or sequence[float], default=1
            Voxel size of the lattice.
        device : torch.device: default='cpu'
            Output tensor device.
        dtype : torch.dtype, default=torch.get_default_dtype()
            Output tensor datatype.

        """
        super().__init__()
        self.shape = shape
        self.mean = mean
        self.absolute = absolute
        self.membrane = membrane
        self.bending = bending
        self.lame = lame
        self.voxel_size = voxel_size
        self.cache_greens = cache_greens
        self.device = device
        if dtype is None or not dtype.is_floating_point:
            dtype = torch.get_default_dtype()
        self.dtype = dtype

    def forward(self, batch=1, **overload):
        """

        Parameters
        ----------
        batch : int, default=1
            Batch size
        overload : dict

        Returns
        -------
        field : (batch, channel, *shape) tensor
            Generated random field

        """

        # get arguments
        shape = overload.get('shape', self.shape)
        mean = overload.get('mean', self.mean)
        voxel_size = overload.get('voxel_size', self.voxel_size)
        dtype = overload.get('dtype', self.dtype)
        device = overload.get('device', self.device)
        backend = dict(dtype=dtype, device=device)

        # sample if parameters are callable
        nb_dim = len(shape)
        voxel_size = utils.make_vector(voxel_size, nb_dim, **backend)
        voxel_size = voxel_size.tolist()
        lame = py.make_list(self.lame, 2)

        if (hasattr(self, '_greens')
                and self._voxel_size == voxel_size
                and self._shape == shape):
            greens = self._greens.to(dtype=dtype, device=device)
        else:
            greens = spatial.greens(
                shape,
                absolute=self.absolute,
                membrane=self.membrane,
                bending=self.bending,
                lame=self.lame,
                voxel_size=voxel_size,
                device=device,
                dtype=dtype)
            if any(lame):
                greens, scale, _ = torch.svd(greens)
                scale = scale.sqrt_()
                greens *= scale.unsqueeze(-1)
            else:
                greens = greens.sqrt_()

            if self.cache_greens:
                self._greens = greens
                self._voxel_size = voxel_size
                self._shape = shape

        sample = torch.randn([2, batch, *shape, nb_dim], **backend)

        # multiply by square root of greens
        if greens.dim() > nb_dim:  # lame
            sample = linalg.matvec(greens, sample)
        else:
            sample = sample * greens.unsqueeze(-1)
            voxel_size = utils.make_vector(voxel_size, nb_dim, **backend)
            sample = sample / voxel_size.sqrt()
        sample = fft.complex(sample[0], sample[1])

        # inverse Fourier transform
        dims = list(range(-nb_dim-1, -1))
        sample = fft.real(fft.ifftn(sample, dim=dims))
        sample *= py.prod(shape)

        # add mean
        sample += mean

        return sample


class _HyperRandomField(Module):
    """A random field whose parameters are randomly sampled"""

    RandomFieldClass = RandomField

    def __init__(self, shape=None,
                 mean='normal', mean_exp=0, mean_scale=0,
                 amplitude='lognormal', amplitude_exp=1, amplitude_scale=1,
                 fwhm='lognormal', fwhm_exp=1, fwhm_scale=1,
                 channel=1, basis=1, device=None, dtype=None):
        """
        The geometry of a random field is controlled by three parameters:
            - `mean` controls the expected value of the field.
            - `amplitude` controls the voxel-wise variance of the field.
            - `fwhm` controls the smoothness of the field.

        Each of these parameter is sampled according to three hyper-parameters:
            - <param>       : distribution family
                              {'normal', 'lognormal', 'uniform', 'gamma', None}
            - <param>_exp   : expected value of the parameter
            - <param>_scale : standard deviation of the parameter

        Parameters
        ----------
        shape : sequence[int]
        mean : {'normal', 'lognormal', 'uniform', 'gamma'}, default='normal'
        mean_exp : float or (channel,) vector_like, default=0
        mean_scale : float or (channel,) vector_like, default=0
        amplitude : {'normal', 'lognormal', 'uniform', 'gamma'}, default='lognormal'
        amplitude_exp : float or (channel,) vector_like, default=1
        amplitude_scale : float or (channel,) vector_like, default=1
        fwhm : {'normal', 'lognormal', 'uniform', 'gamma'}, default='lognormal'
        fwhm_exp : float or (channel,) vector_like, default=1
        fwhm_scale : float or (channel,) vector_like, default=1
        channel : int, default=1
        basis : {0, 1}, default=1
        device : torch.device, optional
        dtype : torch.dtype, optional
        """
        super().__init__()
        self.mean_exp = mean_exp
        self.mean_scale = mean_scale
        self.mean = _get_dist(mean)
        self.amplitude_exp = amplitude_exp
        self.amplitude_scale = amplitude_scale
        self.amplitude = _get_dist(amplitude)
        self.fwhm_exp = fwhm_exp
        self.fwhm_scale = fwhm_scale
        self.fwhm = _get_dist(fwhm)
        self.shape = shape
        self.channel = channel
        self.basis = basis
        self.device = device
        self.dtype = dtype

    def _make_sampler(self, name, **backend):
        exp = getattr(self, name + '_exp')
        scale = getattr(self, name + '_scale')
        dist = getattr(self, name)
        exp = utils.make_vector(exp, **backend)
        scale = utils.make_vector(scale, **backend)
        if dist and (scale > 0).all():
            sampler = dist(exp, scale)
        else:
            sampler = _get_dist('dirac')(exp)
        return sampler

    def forward(self, batch=1, **overload):
        """

        Parameters
        ----------
        batch : int, default=1
            Number of batch elements

        Other Parameters
        ----------------
        shape : sequence[int]
        basis : int
        device : torch.device
        dtype : torch.dtype

        Returns
        -------
        field : (batch, channel, *shape) tensor
            Smooth random field

        """
        shape = overload.get('shape', self.shape)
        channel = overload.get('channel', self.channel)
        dtype = overload.get('dtype', self.dtype)
        device = overload.get('device', self.device)
        dtype = dtype or torch.get_default_dtype()
        backend = dict(dtype=dtype, device=device)

        mean = self._make_sampler('mean', **backend)
        amplitude = self._make_sampler('amplitude', **backend)
        fwhm = self._make_sampler('fwhm', **backend)

        out = torch.empty([batch, channel, *shape], dtype=dtype, device=device)
        for b in range(batch):
            mean1 = mean.sample()
            amplitude1 = amplitude.sample()
            fwhm1 = fwhm.sample()
            field = self.RandomFieldClass(
                shape, mean1, amplitude1, fwhm1, channel,
                basis=self.basis, dtype=dtype, device=device)
            out[b] = field(batch=1)[0]
        return out


class HyperRandomField(_HyperRandomField):
    RandomFieldClass = RandomField


class HyperRandomFieldSpline(_HyperRandomField):
    RandomFieldClass = RandomFieldSpline


class RandomMultiplicativeField(Module):
    """Exponentiated random field with fixed hyper-parameters."""

    def __init__(self, mean=1, amplitude=1, fwhm=5, device=None, dtype=None,
                 sigmoid=False):
        """
        The geometry of a random field is controlled by three parameters:
            - `mean` controls the expected value of the field.
            - `amplitude` controls the voxel-wise variance of the field.
            - `fwhm` controls the smoothness of the field.

        Parameters
        ----------
        mean : float or (channel,) vector_like, default=1
            Mean value.
        amplitude : float or (channel,) vector_like, default=1
            Amplitude of the squared-exponential kernel.
        fwhm : float or (channel,) vector_like, default=5
            Full-width at Half Maximum of the squared-exponential kernel.
        device : torch.device, optional
            Output tensor device.
        dtype : torch.dtype, optional
            Output tensor datatype.
        """

        super().__init__()
        mean = mean.log() if torch.is_tensor(mean) else math.log(mean)
        self.field = RandomFieldSpline(mean=mean, amplitude=amplitude,
                                       fwhm=fwhm, device=device, dtype=dtype)
        self.sigmoid = sigmoid

    def forward(self, shape, **overload):
        """

        Parameters
        ----------
        shape : sequence[int]
            (batch, channel, *spatial)

        Returns
        -------
        bias : (batch, channel, *shape)

        """
        bias = self.field(batch=shape[0], channel=shape[1],
                          shape=shape[2:], **overload)
        if self.sigmoid:
            bias = bias.neg_().exp_().add_(1).reciprocal_()
        else:
            bias = bias.exp_()
        return bias


class HyperRandomMultiplicativeField(Module):
    """Exponentiated random field with randomized hyper-parameters."""

    def __init__(self,
                 mean=None, mean_exp=1, mean_scale=0.1,
                 amplitude='lognormal', amplitude_exp=1, amplitude_scale=10,
                 fwhm='lognormal', fwhm_exp=5, fwhm_scale=2,
                 sigmoid=False, device=None, dtype=None):
        """
        The geometry of a random field is controlled by three parameters:
            - `mean` controls the expected value of the field.
            - `amplitude` controls the voxel-wise variance of the field.
            - `fwhm` controls the smoothness of the field.

        Each of these parameter is sampled according to three hyper-parameters:
            - <param>       : distribution family
                              {'normal', 'lognormal', 'uniform', 'gamma', None}
            - <param>_exp   : expected value of the parameter
            - <param>_scale : standard deviation of the parameter

        Parameters
        ----------
        mean : {'normal', 'lognormal', 'uniform', 'gamma'}, optional
        mean_exp : float or (channel,) vector_like, default=1
        mean_scale : float or (channel,) vector_like, default=0.1
        amplitude : {'normal', 'lognormal', 'uniform', 'gamma'}, default='lognormal'
        amplitude_exp : float or (channel,) vector_like, default=1
        amplitude_scale : float or (channel,) vector_like, default=10
        fwhm : {'normal', 'lognormal', 'uniform', 'gamma'}, default='lognormal'
        fwhm_exp : float or (channel,) vector_like, default=5
        fwhm_scale : float or (channel,) vector_like, default=2
        device : torch.device, optional
        dtype : torch.dtype, optional
        """
        super().__init__()
        self.mean = _get_dist(mean)
        self.mean_exp = mean_exp
        self.mean_scale = mean_scale
        self.amplitude = _get_dist(amplitude)
        self.amplitude_exp = amplitude_exp
        self.amplitude_scale = amplitude_scale
        self.fwhm = _get_dist(fwhm)
        self.fwhm_exp = fwhm_exp
        self.fwhm_scale = fwhm_scale
        self.dtype = dtype or torch.get_default_dtype()
        self.device = device
        self.sigmoid = sigmoid

    def _make_sampler(self, name, **backend):
        exp = getattr(self, name + '_exp')
        scale = getattr(self, name + '_scale')
        dist = getattr(self, name)
        exp = utils.make_vector(exp, **backend)
        scale = utils.make_vector(scale, **backend)
        if dist and (scale > 0).all():
            sampler = dist(exp, scale)
        else:
            sampler = _get_dist('dirac')(exp)
        return sampler

    def forward(self, shape, **overload):
        """

        Parameters
        ----------
        shape : sequence[int]
            (batch, channel, *spatial)

        Returns
        -------
        bias : (batch, channel, *shape)

        """
        dtype = overload.get('dtype', self.dtype)
        device = overload.get('device', self.device)
        backend = dict(dtype=dtype, device=device)

        mean = self._make_sampler('mean', **backend)
        amplitude = self._make_sampler('amplitude', **backend)
        fwhm = self._make_sampler('fwhm', **backend)

        out = torch.empty(shape, dtype=dtype, device=device)
        for b in range(shape[0]):
            mean1 = mean.sample()
            amplitude1 = amplitude.sample()
            fwhm1 = fwhm.sample()
            print('bias:', mean1.item(), amplitude1.item(), fwhm1.item())

            sampler = RandomMultiplicativeField(mean1, amplitude1, fwhm1,
                                                dtype=dtype, device=device,
                                                sigmoid=self.sigmoid)
            out[b] = sampler([1, *shape[1:]])[0]
        return out

import torch
import numpy as np
from scipy.special import gamma as gamma_fn
from sklearn.cluster import KMeans
import math
import argparse


class HMM:
    def __init__(self, data, ins=None, k=5, TM=None, OM=None, full_cov=False, io=True, state_io=False,
                 personalized_io=False, personalized=False, eps=0, min_var=1e-6, device='cpu', priorV=False,
                 priorMu=False, sample_num=1, alpha=10., beta=5., UT=False, var_fill=0.5, VI_diag=False):
        """
        personalized input-output hidden markov model. This class of models considers patient observations that are
        modeled by several possible factors, which are turned on or off using flags. The most complete version of the
        model is x_i,t | z_i,t = k, d_i,t ~ N(mu_k + R_i + (V_k + M_i)*D_i,t, sigma_k) where x_i,t is the observed data,
        z_i,t is the latent state, d_i,t is the observed drug information, R_i is a personalized state effect, V_k is
        a state-based drug effect, M_i is a personalized drug effect, and sigma_k is the covariance.

        :param data: an n x t x d matrix of clinical observations
        :param ins: an n x t matrix of input/drug information (note that this is assumed to be univariate)
        :param k: number of latent states
        :param TM: the time mask, indicates trailing zeros in the observation array
        :param OM: the observation mask, indicates missing observations within the time series (i.e. missed visit)
        :param full_cov: flag indicating if a full covariance matrix should be used, alternatively a diagonal covariance is used
        :param io: flag indicating if the model is an input-output HMM; drugs should not be none if io=True
        :param state_io: flag indicating if input-output effects should be a function of state, if io=True and
        state_io=False, V_k = V for all k. This flag should not be True if io=False
        :param personalized_io: flag indicating if input-output effects should be a function of patient (i.e. M_i is
        'turned on'). This flag should not be True if io=False
        :param personalized: flag indicating if personalized state effects should be applied (i.e. R_i is 'turned on').
        :param eps: prevent division by zero
        :param min_var: set a minimum allowable variance
        :param device: either cpu or cuda
        :param priorV: indicates if priors should be used for the state- and personalized-drug effects
        :param priorMu: indicates if prios should be used for the state-means
        :param sample_num: number of saples used in MC sampling; only 1 sample is currently supported
        :param alpha: parameter of the inverse gamma distribution used as prior for V_k and M_i
        :param beta: parameter of the inverse gamma distribution used as prior for V_k and M_i
        :param UT: parameter to enforce an upper triangular structure for the transition matrix
        :param var_fill: parameter to specify initial guess for variance
        """

        # number of latent states
        self.k = k
        # flag to indicate whether or not to use a full covariance matrix (alternative is diagonal)
        self.full_cov = full_cov
        # flag to indicate whether or not the model is input-output
        self.io = io
        # flag to indicate if io effects should be a function of state
        self.state_io = state_io
        # flag to indicate if io effects should be personalized
        self.perso_io = personalized_io
        # flag to indicate if personalized (non-io) effects should be included
        self.perso = personalized
        # flag to indicate whether or not to use GPU
        self.device = device
        # flag to indicate whether or not to have a prior on V
        self.priorV = priorV
        # flag to indicate whether or not to have a prior on mu
        self.priorMu = priorMu
        # store the parameters of the IG prior
        self.alpha = torch.tensor([alpha], dtype=torch.float64, requires_grad=False, device=self.device).float()
        self.beta = torch.tensor([beta], dtype=torch.float64, requires_grad=False, device=self.device).float()
        # flag to indicate upper triangular structure for transition matrix
        self.ut = UT

        # flag to indicate whether to use diagonal covariance for variational distribution
        self.VI_diag = VI_diag

        # store the data used in analysis
        self.data = data.to(device=self.device)  # n x t x d
        self.n, self.t, self.d = self.data.shape

        #store likelihood (this is the objective if no personalized effects are used)
        self.ll = []

        if self.perso_io and self.perso:
            # case with both personalized state and medication effects
            self.elbo = [] #objective
            self.mu_hat = torch.zeros(self.n, self.d, requires_grad=True, device=self.device)
            # vector for optimization of covariance which is mapped into lower triangular cholesky factor
            if self.VI_diag:
                self.tril_vec = torch.tensor(0.01*np.random.randn(self.n*self.d), requires_grad=True,
                                             device=self.device, dtype=torch.float32)
                self.L_hat = torch.zeros(self.n, self.d, self.d, device=self.device)
                self.L_hat[torch.stack([torch.eye(self.d) for _ in range(self.n)]) == 1] = self.tril_vec
            else:
                self.tril_vec = torch.tensor(0.01*np.random.randn(self.n * int(0.5 * self.d * (self.d + 1))),
                                            requires_grad=True, device=self.device, dtype=torch.float32)
                self.L_hat = torch.zeros(self.n, self.d, self.d, device=self.device)
                self.L_hat[torch.tril(torch.ones(self.n, self.d, self.d)) == 1] = self.tril_vec


            self.nu_hat = torch.zeros(self.n, self.d, requires_grad=True, device=self.device)
            # vector for optimization of covariance which is mapped into lower triangular cholesky factor
            if self.VI_diag:
                self.tril = torch.tensor(0.01 * np.random.randn(self.n * self.d), requires_grad=True,
                                         device=self.device, dtype=torch.float32)
                self.N_hat = torch.zeros(self.n, self.d, self.d, device=self.device)
                self.N_hat[torch.stack([torch.eye(self.d) for _ in range(self.n)]) == 1] = self.tril
            else:
                self.tril = torch.tensor(0.01*np.random.randn(self.n * int(0.5 * self.d * (self.d + 1))),
                                        requires_grad=True, device=self.device, dtype=torch.float32)
                self.N_hat = torch.zeros(self.n, self.d, self.d, device=self.device)
                self.N_hat[torch.tril(torch.ones(self.n, self.d, self.d)) == 1] = self.tril


            self.optimizer = torch.optim.Adam([self.mu_hat, self.tril_vec, self.nu_hat, self.tril], lr=0.001)
        elif self.perso_io:
            # case with personalized medication effects
            self.elbo = [] #objective
            self.mu_hat = torch.zeros(self.n, self.d, requires_grad=True, device=self.device)
            # vector for optimization of covariance which is mapped into lower triangular cholesky factor
            if self.VI_diag:
                self.tril_vec = torch.tensor(0.01 * np.random.randn(self.n * self.d), requires_grad=True,
                                             device=self.device, dtype=torch.float32)
                self.L_hat = torch.zeros(self.n, self.d, self.d, device=self.device)
                self.L_hat[torch.stack([torch.eye(self.d) for _ in range(self.n)]) == 1] = self.tril_vec
            else:
                self.tril_vec = torch.tensor(0.1 * np.random.randn(self.n * int(0.5 * self.d * (self.d + 1))),
                                         requires_grad=True, device=self.device, dtype=torch.float32)
                self.L_hat = torch.zeros(self.n, self.d, self.d, device=self.device)
                self.L_hat[torch.tril(torch.ones(self.n, self.d, self.d)) == 1] = self.tril_vec

            self.optimizer = torch.optim.Adam([self.mu_hat, self.tril_vec], lr=0.001)
        elif self.perso:
            # case with personalized state effects
            self.elbo = [] #objective
            self.nu_hat = torch.zeros(self.n, self.d, requires_grad=True, device=self.device)
            # vector for optimization of covariance which is mapped into lower triangular cholesky factor
            if self.VI_diag:
                self.tril = torch.tensor(0.01 * np.random.randn(self.n * self.d), requires_grad=True,
                                         device=self.device, dtype=torch.float32)
                self.N_hat = torch.zeros(self.n, self.d, self.d, device=self.device)
                self.N_hat[torch.stack([torch.eye(self.d) for _ in range(self.n)]) == 1] = self.tril
            else:
                self.tril = torch.tensor(0.1 * np.random.randn(self.n * int(0.5 * self.d * (self.d + 1))),
                                     requires_grad=True, device=self.device, dtype=torch.float32)
                self.N_hat = torch.zeros(self.n, self.d, self.d, device=self.device)
                self.N_hat[torch.tril(torch.ones(self.n, self.d, self.d)) == 1] = self.tril

            self.optimizer = torch.optim.Adam([self.nu_hat, self.tril], lr=0.001)


        # store the inputs used in analysis
        if self.io:
            self.ins = ins.to(self.device)  # n x t x 1

        # store the time mask
        if TM is None:
            self.tm = torch.ones(self.n, self.t, requires_grad=False, device=self.device)
        else:
            self.tm = TM.to(self.device)  # n x t

        # store the observation mask
        if OM is None:
            self.om = torch.ones(self.n, self.t, requires_grad=False, device=self.device)
        else:
            self.om = OM.to(self.device)  # n x t

        self.eps = eps
        self.min_var = min_var
        self.ini_var = var_fill

        self.sample_num = sample_num


    def initialize_model(self, km_init=True):
        """
        Initializes the parameters of the PIOHMM model
        km_init: flag to indicate if kmeans should be used to initialize the state means
        """
        # All implementations of the model have the parameter set {mu, var, pi, A}

        if km_init:
            # initialize the means using kmeans
            kmeans = KMeans(n_clusters=self.k, init='random').fit(torch.reshape(self.data[:, :, :].cpu(), [self.n*self.t, self.d]))
            mu = torch.tensor(kmeans.cluster_centers_, requires_grad=False, device=self.device).float()
        else:
            # choose k initial points from data to initialize means
            idxs = torch.from_numpy(np.random.choice(self.n, self.k, replace=False))
            mu = self.data[idxs, 0, :]

        if self.full_cov:
            #create k random symmetric positive definite d  x d matrices
            R = 0.1*torch.rand(self.k, self.d, self.d, requires_grad=False)
            var = torch.stack([0.5*(R[i,:,:].squeeze() + torch.t(R[i,:,:].squeeze())) + self.d*torch.eye(self.d)
                                    for i in range(self.k)]).to(self.device)
        else:
            var = torch.Tensor(self.k, self.d,device=self.device).fill_(self.ini_var)

        # uniform prior
        pi = torch.empty(self.k, requires_grad=False, device=self.device).fill_(1. / self.k)

        # transition matrix
        if self.ut:
            A = torch.stack([1./(self.k - i)*torch.ones(self.k, requires_grad=False, device=self.device) for i in range(self.k)])
            A = torch.triu(A)
        else:
            A = torch.stack([1./self.k*torch.ones(self.k, requires_grad=False, device=self.device) for _ in range(self.k)])

        params = {'mu': mu, 'var': var, 'pi': pi, 'A': A}

        # input transformation matrix
        if self.io:
            if self.state_io:
                V = torch.zeros(self.k, self.d, requires_grad=False, device=self.device)
            else:
                V = torch.zeros(self.d, requires_grad=False, device=self.device)
            params['V'] = V

            # variational parameters
            if self.perso_io:
                # transformation matrix prior noise
                # initialize using the mean of the IG distribution
                mnoise = torch.tensor([0.5], device=self.device)
                params['mnoise'] = mnoise

            if self.priorV:
                vnoise = torch.tensor([1.0], device=self.device)
                params['vnoise'] = vnoise

        if self.perso:
            nnoise = torch.tensor([0.5], device=self.device)
            params['nnoise'] = nnoise

            if self.priorMu:
                munoise = torch.tensor([1.0], device=self.device)
                params['munoise'] = munoise

        return params

    def batch_mahalanobis(self, L, x, check=True):
        """
        Computes the squared Mahalanobis distance :math:`\mathbf{x}^\top\mathbf{M}^{-1}\mathbf{x}`
        for a factored :math:`\mathbf{M} = \mathbf{L}\mathbf{L}^\top`.

        Accepts batches for both L and x.
        """
        flat_L = L.unsqueeze(0).reshape((-1,) + L.shape[-2:])
        L_inv = torch.stack([torch.inverse(Li.t()) for Li in flat_L]).view(L.shape).to(self.device)
        batch_val = L_inv.shape[0]

        if check:
            return (torch.stack([x[i, :, :].unsqueeze(-1)*L_inv[i, :, :] for i in range(batch_val)])).sum(-2).pow(2.0).sum(-1)
        else:
            return (torch.stack([x[i, :].unsqueeze(-1) * L_inv[i, :, :] for i in range(batch_val)])).sum(-2).pow(2.0).sum(-1)

    def batch_diag(self, bmat):
        """
        Returns the diagonals of a batch of square matrices.
        """
        return bmat.reshape(bmat.shape[:-2] + (-1,))[..., ::bmat.size(-1) + 1]

    def log_gaussian(self, params, m_sample=None, n_sample=None):
        """
        Returns the density of the model data given the current parameters
        :param params: set of model parameters
        :param m_sample: current sample of m_i, only applicable for perso_io=True
        :param n_sample: current sample of r_i, only applicable for perso=True
        :return: log likelihood at each time point for each possible cluster component, k x n x t
        """
        #unpack params
        mu = params['mu']
        var = params['var']

        log_norm_constant = self.d * torch.log(2 * torch.tensor(math.pi, device=self.device))

        if self.full_cov:
            try:
                # This try statement helps catch issues related to singular covariance, which can be an issue that is difficult to trace
                L = torch.cholesky(var)
            except:
                print(var)
                print(mu)
                print(params['A'])
                print(params['V'])
            r = self.data[None, :, :, :] - mu[:, None, None, :]
            if self.io:
                V = params['V']
                if self.state_io:
                    r = r - V[:, None, None, :]*self.ins[None, :, :, None]
                else:
                    r = r - V*self.ins[:, :, None]

                if self.perso_io:
                    r = r - m_sample[None, :, None, :]*self.ins[None, :, :, None]

            if self.perso:
                r = r - n_sample[None, :, None, :]

            md = self.batch_mahalanobis(L, r)
            log_det = 2*self.batch_diag(L).abs().log().sum(-1).to(self.device)
            log_p = -0.5* (md + log_norm_constant  + log_det[:, None, None])

        else:
            r = self.data[None, :, :, :] - mu[:, None, None, :]
            if self.io:
                V = params['V']
                if self.state_io:
                    r = r - V[:, None, None, :]*self.ins[None, :, :, None]
                else:
                    r = r - V*self.ins[:, :, None]

                if self.perso_io:
                    r = r - m_sample[None, :, None, :]*self.ins[:, :, None]
            if self.perso:
                r = r - n_sample[None, :, None, :]

            r = r**2
            log_p = -0.5 * (var.log()[:, None, None, :] + r / var[:, None, None, :])
            log_p = log_p + log_norm_constant
            log_p = log_p.sum(-1)

        return log_p

    def log_gaussian_prior(self, rv, mu, L):
        """
        Returns the probability of random varaible rv with mean mu and variance var; does not support full covariance
        structure
        :param rv d
        :param mu d
        :param L cholesky matrix for covariance of RV
        :return: log probability
        """
        d = np.shape(rv)[0]
        log_norm_constant = -0.5 * d * torch.log(2 * torch.tensor(math.pi, device=self.device))
        r = rv - mu
        md = self.batch_mahalanobis(L, r, check=False)
        log_det = self.batch_diag(L).abs().log().sum(-1).to(self.device)
        log_p = -0.5 * md + log_norm_constant - log_det

        return log_p

    def log_ig(self, noise):
        """
        Returns the probability of the inverse gamma prior
        :return:
        """
        
        log_ig = self.alpha * torch.log(self.beta) - torch.log(gamma_fn(self.alpha.cpu())).to(self.device) - \
                 (self.alpha + 1.) * torch.log(noise) - self.beta/noise

        return log_ig

    def get_likelihoods(self, params, log=True, m_sample=None, n_sample=None):
        """
        :param log: flag to indicate if likelihood should be returned in log domain
        :return likelihoods: (k x n x t)
        """

        log_likelihoods = self.log_gaussian(params, m_sample, n_sample)

        if not log:
            log_likelihoods.exp_()

        #multiply the liklihoods by the observation mask
        return (log_likelihoods*self.om[None, :, :]).to(self.device)

    def get_exp_data(self, mu, var, V=None, m_sample=None, n_sample=None):
        """
        Function to calculate the expectation of the conditional log-likelihood with respect to the variational
        approximation q(M|X)
        :return: expectation of the conditional log-likelihood wrt the variational approximation
        """
        if self.full_cov:
            L = torch.cholesky(var)
        else:
            L = torch.zeros(self.k, self.d, self.d)
            for i in range(self.k):
                L[i, :, :] = torch.diag(torch.sqrt(var[i, :]))
        r = self.data[None, :, :, :] - mu[:, None, None, :]
        if self.io:
            r = r - V[:, None, None, :] * self.ins[None, :, :, None]
        if self.perso_io:
            r = r - m_sample[None, :, None, :]*self.ins[None, :, :, None]
        if self.perso:
            r = r - n_sample[None, :, None, :]

        const = self.d*torch.log(2*torch.tensor(math.pi, device=self.device)) #scalar
        logdet = 2*self.batch_diag(L).abs().log().sum(-1).to(self.device) #k
        md1 = self.batch_mahalanobis(L, r) #k x n x t

        out = -0.5*(const + logdet[:, None, None] + md1)

        return out

    def get_exp_M(self, mnoise):
        """
        Function to calculate the expectation of the prior with respect to the variational approximation q(M|X)
        :return: expectation of the prior on M wrt the variational approximation
        """

        out = -self.d*self.n/2*torch.log(2*torch.tensor(math.pi, device=self.device)*mnoise) - \
              (1/mnoise/2*torch.einsum('kij, kij -> k', [self.L_hat, self.L_hat])).sum() - \
              (1/mnoise/2*torch.einsum('ij,ij->i', [self.mu_hat, self.mu_hat])).sum()
        return out

    def get_exp_V(self, V, vnoise):
        """
        Function to calculate the expectation of the prior with respect to the variational approximation q(M|X)
        :return: expectation of the prior on M wrt the variational approximation
        """

        out = -self.d * self.k / 2 * torch.log(2 * torch.tensor(math.pi, device=self.device) * vnoise) - \
              (1 / vnoise / 2 * torch.einsum('ij,ij->i', [V, V])).sum()
        return out

    def get_exp_Mtilde(self, nnoise):
        """
        Function to calculate the expectation of the prior with respect to the variational approximation q(M|X)
        :return: expectation of the prior on M wrt the variational approximation
        """

        out = -self.d * self.n / 2 * torch.log(2 * torch.tensor(math.pi, device=self.device) * nnoise) - \
              (1 / nnoise / 2 * torch.einsum('kij, kij -> k', [self.N_hat, self.N_hat])).sum() - \
              (1 / nnoise / 2 * torch.einsum('ij,ij->i', [self.nu_hat, self.nu_hat])).sum()
        return out


    def exp_log_joint(self, params, e_out, samples):
        """
        Function to calculate the expectation of the joint likelihood with respect to the variational approximation
        :return:
        """
        #unpack parameters
        pi = params['pi']
        A = params['A']
        logA = A.log()

        logA[torch.isinf(logA)] = 0

        mu = params['mu']
        var = params['var']
        m_sample = samples['m_sample']
        n_sample = samples['n_sample']

        gamma = e_out['gamma']
        xi = e_out['xi']


        if self.io:
            V = params['V']
            lj = (gamma[:, :, 0].exp() * pi[:, None].log()).sum() + \
                 (xi.exp() * logA[:, :, None, None] * self.om[None, None, :, 1:]).sum() + \
                 (self.get_exp_data(mu, var, V=V, m_sample=m_sample, n_sample=n_sample) * gamma.exp() * self.om[None, :, :]).sum()
        else:
            lj = (gamma[:, :, 0].exp() * pi[:, None].log()).sum() + \
                 (xi.exp() * logA[:, :, None, None] * self.om[None, None, :, 1:]).sum() + \
                 (self.get_exp_data(mu, var, m_sample=m_sample, n_sample=n_sample) * gamma.exp() * self.om[None, :, :]).sum()


        if self.perso:
            nnoise = params['nnoise']
            lj = lj + self.get_exp_Mtilde(nnoise)

        if self.perso_io:
            mnoise = params['mnoise']
            lj = lj + self.get_exp_M(mnoise)

        if self.priorV:
            vnoise = params['vnoise']
            mnoise = params['mnoise']
            V = params['V']
            lj = lj + self.log_ig(vnoise) + self.log_ig(mnoise) + self.get_exp_V(V, vnoise)

        if self.priorMu:
            munoise = params['munoise']
            nnoise = params['nnoise']
            lj = lj + self.log_ig(munoise) + self.log_ig(nnoise) + self.get_exp_V(mu, munoise)
        
        return lj

    def entropy(self, e_out):
        """
        Function to calculate the entropy
        :return:
        """
        gamma = e_out['gamma']
        xi = e_out['xi']

        gamma_sum = gamma[:, :, 0].exp()*gamma[:, :, 0]*self.om[None, :, 0]

        if self.ut:
            xi_sum = 0
            for i in range(self.n):
                for j in range(1, self.t):
                    xi_sum = xi_sum + (torch.triu(xi[:,:,i,j-1]).exp()*torch.triu(xi[:,:,i,j-1])*self.om[None, None, i, j]).sum()
            xi_sum = xi_sum - (xi.exp()*gamma[:, None, :, :-1]*self.om[None, None, :, 1:]).sum()
        else:
            xi_sum = (xi.exp()*(xi - gamma[:, None, :, :-1])*self.om[None, None, :, 1:]).sum()

        et = - gamma_sum.sum() - xi_sum

        if self.perso_io:
            logdet = 2*self.batch_diag(self.L_hat).abs().log().sum(-1)
            diffe = 0.5*(logdet + self.d*np.log(2*torch.tensor(math.pi)) + self.d).sum()
            et = et + diffe
        if self.perso:
            logdet = 2*self.batch_diag(self.N_hat).abs().log().sum(-1)
            diffe = 0.5*(logdet + self.d*np.log(2*torch.tensor(math.pi)) + self.d).sum()
            et = et + diffe
        return et

    def variational_obj(self, params, e_out, samples):
        """
        Function to calculate the elbo using the expectation of the joint likelihood and the entropy
        :return:
        """
        obj1 = -self.exp_log_joint(params, e_out, samples)
        obj2 = -self.entropy(e_out)

        self.elbo.append((obj1 + obj2).item())

        return obj1 + obj2

    def baseline_variational_obj(self, params, e_out, samples):
        """
        Function to calculate the elbo when only one time point has been observed
        """
        # unpack parameters
        pi = params['pi']
        A = params['A']
        logA = A.log()

        logA[torch.isinf(logA)] = 0

        mu = params['mu']
        var = params['var']
        m_sample = samples['m_sample']
        n_sample = samples['n_sample']

        gamma = e_out['gamma']

        if self.io:
            V = params['V']
            lj = (gamma[:, :, 0].exp() * pi[:, None].log()).sum() + \
                 (self.get_exp_data(mu, var, V=V, m_sample=m_sample, n_sample=n_sample) * gamma.exp() * self.om[None, :, :]).sum()
        else:
            lj = (gamma[:, :, 0].exp() * pi[:, None].log()).sum() + \
                 (self.get_exp_data(mu, var, m_sample=m_sample, n_sample=n_sample) * gamma.exp() * self.om[None, :, :]).sum()


        if self.perso:
            nnoise = params['nnoise']
            lj = lj + self.get_exp_Mtilde(nnoise)

        if self.perso_io:
            mnoise = params['mnoise']
            lj = lj + self.get_exp_M(mnoise)

        if self.priorV:
            vnoise = params['vnoise']
            mnoise = params['mnoise']
            V = params['V']
            lj = lj + self.log_ig(vnoise) + self.log_ig(mnoise) + self.get_exp_V(V, vnoise)

        if self.priorMu:
            munoise = params['munoise']
            nnoise = params['nnoise']
            lj = lj + self.log_ig(munoise) + self.log_ig(nnoise) + self.get_exp_V(mu, munoise)

        gamma_sum = gamma[:, :, 0].exp() * gamma[:, :, 0] * self.om[None, :, 0]

        et = - gamma_sum.sum()

        if self.perso_io:
            logdet = 2 * self.batch_diag(self.L_hat).abs().log().sum(-1)
            diffe = 0.5 * (logdet + self.d * np.log(2 * torch.tensor(math.pi)) + self.d).sum()
            et = et + diffe
        if self.perso:
            logdet = 2 * self.batch_diag(self.N_hat).abs().log().sum(-1)
            diffe = 0.5 * (logdet + self.d * np.log(2 * torch.tensor(math.pi)) + self.d).sum()
            et = et + diffe

        self.elbo.append((-lj + -et).item())

        return -lj - et



    def forward(self, likelihood, params):
        """
        Calculate the forward pass of the EM algorithm for the HMM (Baum-Welch)
        :param likelihood: log-likelihood of the data for the current parameters
        :param params: current model parameters
        :return: k x n x t log alpha's and the n x t scaling factors
        Note this implementation uses the rescaled 'alpha-hats'
        """
        #unpack params
        pi = params['pi']
        A = params['A']
        logA = A.log()

        alpha = torch.zeros(self.k, self.n, self.t, device=self.device)
        scaling_factor = torch.zeros( self.n, self.t, device=self.device)
        a = pi[:, None].log() + likelihood[:, :, 0]
        scaling_factor[:, 0] = torch.logsumexp(a, dim=0)
        alpha[:, :, 0] = a - scaling_factor[:, 0]
        for i in range(1, self.t):
            asample = alpha[:, :, i-1] #this is the previous time point alpha, we need this for the recursion
            #we'll use the log-sum-exp trick for stable calculation
            a = likelihood[:, :, i] + torch.logsumexp(asample[:, None, :] + logA[:, :, None], dim=0)
            scaling_factor[:, i] = torch.logsumexp(a, dim=0)
            alpha[:, :, i] = a - scaling_factor[:, i]

        #multiply the final results with the time mask to reset missing values to zero
        alpha = alpha*self.tm[None, :, :]
        scaling_factor = scaling_factor*self.tm

        return alpha, scaling_factor #note that this is log alpha and log scaling factor

    def backward(self, likelihood, params, scaling_factor):
        '''
        Calaculate the backward pass of the EM algorithm for the HMM (Baum-Welch)
        :param likelihood: log-likelihood of the data for the current parameters
        :param params: current model parameters
        :param scaling_factor: scaling factors calculated during the forward pass; required for numerical stability
        :return: k x n x t log beta's
        Note this implementation uses the rescaled 'beta-hats'
        '''
        #unpack params
        logA = params['A'].log()

        beta = torch.zeros(self.k, self.n, self.t, device=self.device)
        for i in range(self.t-2, -1, -1):
            bsample = beta[:, :, i+1] #this is the next time point beta, we need this for the recusion
            #we'll use the log-sum-exp trick for stable calculation
            b = torch.logsumexp(bsample[None, :, :] + logA[:, :, None] + likelihood[None, :, :, i+1], dim=1)
            tmi = self.tm[:, i+1]
            beta[:, :, i] = (b - scaling_factor[:, i+1])*tmi[None, :]
        return beta #note that this is log beta

    def e_step(self, params, fixSample=False):
        '''
        'expectation step' for the EM algorithm (Baum-Welch)
        :return: updates gamma, xi and the log-likelihood
        '''

        # default setting is to assume no personalized effects
        m_sample = None
        n_sample = None
        if self.perso_io:
            # sample from the variational approximation of M_i
            if fixSample:
                m_sample = self.mu_hat
            else:
                e_sample = torch.randn(self.n, self.d, device=self.device)
                m_sample = torch.einsum('ijk,ik->ij', [self.L_hat, e_sample]) + self.mu_hat
        if self.perso:
            if fixSample:
                n_sample = self.nu_hat
            else:
                e_sample = torch.randn(self.n, self.d, device=self.device)
                n_sample = torch.einsum('ijk,ik->ij', [self.N_hat, e_sample]) + self.nu_hat

        likelihood = self.get_likelihoods(params, m_sample=m_sample, n_sample=n_sample)

        alpha, scaling_factor = self.forward(likelihood, params)
        #NB: the exponentiated alpha sum over the first dimension should be one
        #print('alpha check:',alpha)
        beta = self.backward(likelihood, params, scaling_factor)
        #the expontiated beta sum over the first dimension should be numerically well-behaved
        #print('beta check:', beta)

        gamma = alpha + beta #note this is log gamma

        logA = params['A'].log()

        xi = alpha[:, None, :, :-1] + beta[None, :, :, 1:] + likelihood[None, :, :, 1:] + \
                      logA[:, :, None, None] - scaling_factor[None, None, :, 1:] #note this is log xi
        #Something about this is causing the upper triangular version to fail
        #xi = xi*self.tm[None, None, :, 1:] #note that this is log xi

        pX = scaling_factor.sum()

        e_out = {'xi': xi, 'gamma': gamma, 'pX': pX}
        samples = {'m_sample': m_sample, 'n_sample': n_sample}

        if self.perso_io or self.perso:
            self.optimizer.zero_grad()
            self.variational_obj(params, e_out, samples).backward(retain_graph=True)
            self.optimizer.step()
        if self.perso_io:
            #update the variational parameters mu_hat and L_hat using gradient descent
            if self.VI_diag:
                self.L_hat[torch.stack([torch.eye(self.d) for _ in range(self.n)]) == 1] = self.tril_vec
            else:
                self.L_hat[torch.tril(torch.ones(self.n, self.d, self.d)) == 1] = self.tril_vec

        if self.perso:
            if self.VI_diag:
                self.N_hat[torch.stack([torch.eye(self.d) for _ in range(self.n)]) == 1] = self.tril
            else:
                self.N_hat[torch.tril(torch.ones(self.n, self.d, self.d)) == 1] = self.tril

        return e_out, params, samples

    def m_step(self, e_out, params, samples):
        '''
        'maximization step' for the EM algorithm (Baum-Welch)
        :return: updates mu_k, sigma_k, V, A, pi
        '''

        with torch.no_grad():
            #un-pack parameters
            gamma = e_out['gamma']
            xi = e_out['xi']

            var = params['var']

            if self.io:
                V = params['V']
                if self.priorV:
                    vnoise = params['vnoise']

            if self.perso_io:
                m_sample = samples['m_sample']

            if self.perso:
                n_sample = samples['n_sample']
                if self.priorMu:
                    munoise = params['munoise']

            # compute `N_k` the proxy "number of points" assigned to each distribution.
            # gamma is k x n x t
            N_k1 = ((gamma[:, :, 0].exp())*self.om[None, :, 0]).sum(1)
            #print(N_k1.sum())
            N_k = ((gamma.exp())*self.om[None, :, :]).sum(-1).sum(-1)
            #print(N_k)

            # get the means by taking the weighted combination of points
            r = self.data
            if self.io:
                if self.state_io:
                    r = r - V[:, None, None, :] * self.ins[None, :, :, None]
                else:
                    r = r - V * self.ins[:, :, None]

                if self.perso_io:
                    r = r - m_sample[None, :, None, :] * self.ins[None, :, :, None]
                if self.perso:
                    r = r - n_sample[None, :, None, :]
            else:
                if self.perso:
                    r = r - n_sample[:, None, :]
            if self.priorMu:
                if self.full_cov:
                    if self.state_io:
                        num = torch.einsum('ijk,ijkl->il', [gamma.exp()*self.om[None, :, :], r*self.om[:, :, None]])
                    else:
                        num = torch.einsum('ijk,jkl->il', [gamma.exp()*self.om[None, :, :], r*self.om[:, :, None]])
                    denom = torch.einsum('i, ijk->ijk', [torch.sum(gamma.exp()*self.om[None, :, :], (1,2)),
                                                        torch.stack([torch.eye(self.d, device=self.device) for _ in range(self.k)])]) + var/munoise
                    MU_LU = torch.lu(denom)
                    mu = torch.lu_solve(num, *MU_LU)
                else:
                    if self.state_io:
                        mu = torch.einsum('ijk,ijkl->il', [gamma.exp() * self.om[None, :, :], r * self.om[:, :, None]])/(torch.sum(gamma.exp(), (1,2)) + var/munoise)
                    else:
                        mu = torch.einsum('ijk,jkl->il', [gamma.exp() * self.om[None, :, :], r * self.om[:, :, None]])/(torch.sum(gamma.exp(), (1,2))[:, None] + var/munoise)
            else:
                if self.state_io:
                    mu = torch.einsum('ijk,ijkl->il', [gamma.exp()*self.om[None, :, :], r*self.om[:, :, None]])
                    mu = mu / (N_k[:, None] + self.eps)
                else:
                    mu = torch.einsum('ijk,jkl->il', [gamma.exp()*self.om[None, :, :], r*self.om[:, :, None]])
                    mu = mu / (N_k[:, None] + self.eps)

            # update the matrix which tansforms the drug information
            if self.io:
                r = self.data - mu[:, None, None, :]
                if self.perso_io:
                    r = r - m_sample[None, :, None, :] * self.ins[None, :, :, None]
                if self.perso:
                    r = r - n_sample[None, :, None, :]
                if self.priorV:
                    num = torch.einsum('ijk,ijkl->il', [gamma.exp() * self.om[None, :, :],
                                                        r * self.ins[None, :, :, None] * self.om[None, :, :, None]])

                    denom = torch.einsum('i,ijk->ijk', [torch.sum(gamma.exp()*self.ins[None, :, :]**2*self.om[None, :, :], (1, 2)), torch.stack([torch.eye(self.d, device=self.device) for _ in range(self.k)])]) + \
                            var/vnoise
                    V_LU = torch.lu(denom)
                    V = torch.lu_solve(num, *V_LU)
                else:
                    if self.state_io:
                        V = torch.einsum('ijk,ijkl->il', [gamma.exp()*self.om[None, :, :],
                                                          r * self.ins[None, :, :, None]*self.om[None, :, :, None]])
                        denom = torch.sum(gamma.exp()*self.ins[None, :, :]**2*self.om[None, :, :], (1, 2))
                        V = V / denom[:, None]
                    else:
                        V = torch.einsum('ijk,ijkl->l', [gamma.exp() * self.om[None, :, :],
                                                         r * self.ins[:, :, None] * self.om[None, :, :, None]])
                        V = V / torch.sum(((gamma.exp()) * self.tm[None, :, :]) * (
                                    (self.ins[None, :, :] * self.om[None, :, :]) ** 2))

            # compute the diagonal covar. matrix, by taking a weighted combination of
            # the each point's square distance from the mean
            r = self.data - mu[:, None, None]
            if self.io:
                if self.state_io:
                    r = r - V[:, None, None, :]*self.ins[None, :, :, None]
                else:
                    r = r - V[None, None, None, :] * self.ins[None, :, :, None]
                if self.perso_io:
                    r = r - m_sample[None, :, None, :]*self.ins[None, :, :, None]
            if self.perso:
                r = r - n_sample[None, :, None, :]
            r = r * self.om[None, :, :, None]
            ### CHECK DERIVATION HERE
            if self.full_cov:
                if self.perso_io:
                    var = ((gamma[:, :, :, None, None].exp()) * self.om[None, :, :, None, None]) * (torch.einsum(
                        'ijkl,ijkm->ijklm', [r, r]))
                else:
                    var = ((gamma[:, :, :, None, None].exp()) * self.om[None, :, :, None, None])\
                          * torch.einsum('ijkl,ijkm->ijklm', [r, r])
                var = var.sum(1).sum(1) / (N_k[:, None, None] + self.eps)

                # add variance ridge to prevent non psd covariance matrices
                var = torch.stack([var[i, :, :] + 1e-4*torch.eye(self.d).to(self.device) for i in range(self.k)])

            else:
                var = torch.einsum('ijk,ijkl->il', [(gamma.exp())*self.om[None, :, :], r**2])
                var = var / (N_k[:, None] + self.eps)
                var = torch.clamp(var, min=self.min_var)

            if self.perso_io:
                # compute the prior mnoise
                if self.priorV:
                    mnoise = 1/(2*self.alpha + 2 + self.n*self.d)*\
                             (2*self.beta + (torch.einsum('ij,ij->i', [m_sample, m_sample])).sum())
                else:
                    mnoise = 1/self.n/self.d*(torch.einsum('ij,ij->i', [m_sample, m_sample])).sum()

            if self.priorV:
                vnoise = 1/(2*self.alpha + 2 + self.d*self.k)*\
                         (2*self.beta + (torch.einsum('ij, ij ->i', [V, V])).sum())
            # CHECK DERIVATION HERE
            if self.perso:
                if self.priorMu:
                    nnoise = 1/(2*self.alpha + 2 + self.n*self.d)*\
                             (2*self.beta + (torch.einsum('ij,ij->i', [n_sample, n_sample])).sum())
                else:
                    nnoise = 1 / self.n / self.d * (torch.einsum('ij,ij->i', [n_sample, n_sample])).sum()
            if self.priorMu:
                munoise = 1/(2*self.alpha + 2 + self.d*self.k)*\
                          (2*self.beta + (torch.einsum('ij, ij-> i', [mu, mu])).sum())

            # recompute the mixing probabilities
            pi = N_k1 / N_k1.sum() + self.eps

            # recompute the transition matrix
            # xi is k x k x n x t - 1
            logA = torch.zeros((self.k, self.k))
            for i in range(self.k):
                for j in range(self.k):
                    logA[i, j] = torch.logsumexp(torch.masked_select(xi[i, j, :, :], self.om[:, 1:].byte()), dim=-1) - \
                                      torch.logsumexp(torch.masked_select(xi[i, :, :, :], self.om[None, :, 1:].byte()), dim=-1)
            A = logA.exp()
            if self.ut:
                A = logA.exp() + self.eps*torch.triu(torch.ones(self.k, self.k))
            else:
                A = logA.exp() + self.eps

            # reset dimensionality
            # mu = mu.squeeze(1)
            # var = var.squeeze(1)
            # pi = pi.squeeze()

            params = {'mu': mu.to(self.device), 'var': var.to(self.device), 'pi': pi.to(self.device), 'A': A.to(self.device)}

            if self.io:
                params['V'] = V.to(self.device)
                if self.perso_io:
                    params['mnoise'] = mnoise.to(self.device)
            if self.perso:
                params['nnoise'] = nnoise.to(self.device)
            if self.priorV:
                params['vnoise'] = vnoise.to(self.device)
            if self.priorMu:
                params['munoise'] = munoise.to(self.device)


        # #
        #print('mu_i: ' , self.mu_hat[0, :])
        # print('var shape:', self.var)
        #print('A:', A)
        #print('pi:', pi)
        #print('V: ', V)
        #print('mu:', mu)
        #print('var:', var)
        # print('mu_hat shape:', self.mu_hat)
        # print('L_hat shape:', self.L_hat)
        return params

    def learn_model(self, num_iter=1000, use_cc=False, cc=1e-6, intermediate_save=True, load_model=False,
                    model_name=None):

        if load_model:
            load_params = torch.load(model_name)
            A = load_params['A']
            mu = load_params['mu']
            var = load_params['var']
            pi = load_params['pi']
            V = load_params['V']
            params = {'mu': mu, 'var': var, 'pi': pi, 'A': A, 'V': V}
            # variational parameters
            if self.perso_io:
                # transformation matrix prior noise
                # initialize using the mean of the IG distribution
                mnoise = torch.tensor([1.5], device=self.device)
                params['mnoise'] = mnoise



        else:
            params = self.initialize_model()
        prev_cost = float('inf')

        for _ in range(num_iter):
            if intermediate_save:
                if _ % 500 == 0:
                    print('Iteration ', _)
                    if self.device[:4] == 'cuda':
                        print(torch.cuda.get_device_name(0))
                        print('Memory Usage:')
                        print('Allocated:', round(torch.cuda.memory_allocated(0)/1024**3,1), 'GB')
                        print('Cached:   ', round(torch.cuda.memory_cached(0)/1024**3,1), 'GB')
                    torch.save({'params': params, 'elbo': self.elbo, 'entropy': self.ent, 'exp_ll': self.ell,
                                'log_prob': self.ll, 'mi': self.mu_hat, 'Li': self.L_hat}, '../results/PD_HMM_Model_iter' + str(_) + '_k' + str(self.k) + '.pkl')

            #e-step, calculate the 'responsibilities'
            e_out, params, samples = self.e_step(params)

            # compute the cost and check for convergence
            obj = e_out['pX'].item()
            self.ll.append(obj)
            if use_cc:
                diff = prev_cost - obj
                if np.abs(diff) < cc:
                    print('Breaking ', _)
                    break
                prev_cost = obj

            #m-step, update the parameters
            params = self.m_step(e_out, params, samples)

        if self.perso_io and self.perso:
            return params, e_out, self.ll, self.elbo, self.mu_hat, self.L_hat, self.nu_hat, self.N_hat
        elif self.perso:
            return params, e_out, self.ll, self.elbo, self.nu_hat, self.N_hat
        elif self.perso_io:
            return params, e_out, self.ll, self.elbo, self.mu_hat, self.L_hat
        else:
            return params, e_out, self.ll

    def est_test_pX(self, params):

        likelihood = self.get_likelihoods(params, fixSample=True)

        alpha, scaling_factor = self.forward(likelihood, params)
        # NB: the exponentiated alpha sum over the first dimension should be one
        # print('alpha check:',alpha)
        # beta = self.backward(likelihood, params, scaling_factor)
        # the expontiated beta sum over the first dimension should be numerically well-behaved
        # print('beta check:', beta)
        # self.gamma = alpha + beta  # note this is log gamma

        # self.xi = alpha[:, None, :, :-1] + beta[None, :, :, 1:] + likelihood[None, :, :, 1:] + \
        #          self.logA[:, :, None, None] - scaling_factor[None, None, :, 1:]  # note this is log xi
        # self.xi = self.xi * self.tm[None, None, :, 1:]  # note that this is log xi

        return scaling_factor.sum()

    def learn_baseline_vi_params(self, params, num_iter=1000, intermediate_save=False):
        for _ in range(num_iter):
            # default setting is to assume no personalized effects
            m_sample = None
            n_sample = None
            if self.perso_io:
                e_sample = torch.randn(self.n, self.d, device=self.device)
                m_sample = torch.einsum('ijk,ik->ij', [self.L_hat, e_sample]) + self.mu_hat
            if self.perso:
                e_sample = torch.randn(self.n, self.d, device=self.device)
                n_sample = torch.einsum('ijk,ik->ij', [self.N_hat, e_sample]) + self.nu_hat

            likelihood = self.get_likelihoods(params, m_sample=m_sample, n_sample=n_sample)
            pi = params['pi']
            gamma = pi[:, None, None].log() + likelihood
            e_out = {'gamma': gamma}
            samples = {'m_sample': m_sample, 'n_sample': n_sample}

            if self.perso_io or self.perso:
                self.optimizer.zero_grad()
                self.baseline_variational_obj(params, e_out, samples).backward(retain_graph=True)
                self.optimizer.step()
            if self.perso_io:
                # update the variational parameters mu_hat and L_hat using gradient descent
                if self.VI_diag:
                    self.L_hat[torch.stack([torch.eye(self.d) for _ in range(self.n)]) == 1] = self.tril_vec
                else:
                    self.L_hat[torch.tril(torch.ones(self.n, self.d, self.d)) == 1] = self.tril_vec

            if self.perso:
                if self.VI_diag:
                    self.N_hat[torch.stack([torch.eye(self.d) for _ in range(self.n)]) == 1] = self.tril
                else:
                    self.N_hat[torch.tril(torch.ones(self.n, self.d, self.d)) == 1] = self.tril

        if self.perso_io and self.perso:
            return params, e_out, self.ll, self.elbo, self.mu_hat, self.L_hat, self.nu_hat, self.N_hat
        elif self.perso:
            return params, e_out, self.ll, self.elbo, self.nu_hat, self.N_hat
        elif self.perso_io:
            return params, e_out, self.ll, self.elbo, self.mu_hat, self.L_hat
        else:
            return params, e_out, self.ll

    def learn_vi_params(self, params, num_iter=1000, intermediate_save=False):

        for _ in range(num_iter):
            if intermediate_save:
                if _ % 500 == 0:
                    print('Iteration ', _)
                    if self.device[:4] == 'cuda':
                        print(torch.cuda.get_device_name(0))
                        print('Memory Usage:')
                        print('Allocated:', round(torch.cuda.memory_allocated(0)/1024**3,1), 'GB')
                        print('Cached:   ', round(torch.cuda.memory_cached(0)/1024**3,1), 'GB')
                    torch.save({'params': params, 'elbo': self.elbo, 'entropy': self.ent, 'exp_ll': self.ell,
                                'log_prob': self.ll, 'mi': self.mu_hat, 'Li': self.L_hat}, '../results/PD_HMM_Model_iter' + str(_) + '_k' + str(self.k) + '.pkl')

            #e-step, calculate the 'responsibilities'
            e_out, params, samples = self.e_step(params)

        if self.perso_io and self.perso:
            return params, e_out, self.ll, self.elbo, self.mu_hat, self.L_hat, self.nu_hat, self.N_hat
        elif self.perso:
            return params, e_out, self.ll, self.elbo, self.nu_hat, self.N_hat
        elif self.perso_io:
            return params, e_out, self.ll, self.elbo, self.mu_hat, self.L_hat
        else:
            return params, e_out, self.ll

    def calc_pX(self, params, num_samples=1, importance_sampling=False, mu_hat = None, nu_hat=None, L_hat=None,
                N_hat = None, fixSample=False):
        '''
        Function to calculate the test log likelihood
        :param params:
        :param iter:
        :param returnVars:
        :return:
        '''

        if importance_sampling:
            px = torch.zeros(num_samples, self.n)
            for i in range(num_samples):
                if self.perso_io:
                    e_sample = torch.randn(self.n, self.d, device=self.device)
                    m_sample = torch.einsum('ijk,ik->ij', [L_hat, e_sample]) + mu_hat
                    L_prior = torch.stack([(params['mnoise'].sqrt()*torch.eye(self.d).to(self.device)) for _ in range(self.n)]).to(self.device)

                    sample_weight_m = (self.log_gaussian_prior(m_sample, torch.zeros(m_sample.shape).to(self.device), L_prior) - \
                                     self.log_gaussian_prior(m_sample, mu_hat, L_hat))
                else:
                    m_sample = None
                    sample_weight_m = 0

                if self.perso:
                    e_sample = torch.randn(self.n, self.d, device=self.device)
                    n_sample = torch.einsum('ijk,ik->ij', [N_hat, e_sample]) + nu_hat
                    N_prior = torch.stack([(params['nnoise'].sqrt()*torch.eye(self.d)).to(self.device) for _ in range(self.n)]).to(self.device)

                    sample_weight_n = (self.log_gaussian_prior(n_sample, torch.zeros(n_sample.shape).to(self.device), N_prior) -
                                     self.log_gaussian_prior(n_sample, nu_hat, N_hat))
                else:
                    n_sample = None
                    sample_weight_n = 0

                likelihood = self.get_likelihoods(params, m_sample=m_sample, n_sample=n_sample)
                alpha, scaling_factor = self.forward(likelihood, params)

                # print((scaling_factor*sample_weight[:, None]).sum())
                px[i, :] = (scaling_factor.sum(-1) + sample_weight_m + sample_weight_n)
            out = (torch.logsumexp(px, 0) - np.log(num_samples))

        elif fixSample:
            likelihood = self.get_likelihoods(params, m_sample=mu_hat, n_sample=nu_hat)
            alpha, scaling_factor = self.forward(likelihood, params)

            out = scaling_factor


        else:
            px = torch.zeros(num_samples, self.n)
            for i in range(num_samples):
                if self.perso_io:
                    mnoise = params['mnoise']
                    # sample from the  sampled from the MLE params
                    m_sample = mnoise.sqrt()*torch.randn(self.n, self.d, device=self.device)
                else:
                    m_sample = None
                if self.perso:
                    nnoise = params['nnoise']
                    n_sample = nnoise.sqrt()*torch.randn(self.n, self.d, device=self.device)
                else:
                    n_sample = None
                likelihood = self.get_likelihoods(params, m_sample=m_sample, n_sample=n_sample)
                alpha, scaling_factor = self.forward(likelihood, params)
                px[i, :] = scaling_factor.sum(-1)
            out = (torch.logsumexp(px, 0) - np.log(num_samples))

        return out

    def predict_sequence(self, params, m_sample=None, n_sample=None):
        '''

        :return:
        '''
        likelihood = self.get_likelihoods(params, m_sample=m_sample, n_sample=n_sample)
        # print('likelihood:', likelihood)
        mps, omega, psi = self.viterbi(likelihood, params)
        #mps = self.viterbi(likelihood, params)
        return mps, omega, psi

    # def viterbi(self, likelihood, params):
    # note that this code was used for testing viterbi alg.
    #     logA = params['A'].log()
    #     pi = params['pi']
    #
    #     T1 = torch.zeros(self.k, self.n, self.t).to(self.device)
    #     T2 = torch.zeros(self.k, self.n, self.t).to(self.device)
    #     mps = torch.zeros(self.n, self.t).to(self.device)
    #
    #     T1[:,:,0] = pi[:, None].log() + likelihood[:,:,0]
    #     for i in range(1, self.t):
    #         for j in range(self.k):
    #             T1[j, :, i], T2[j, :, i] = torch.max(logA[:, j, None, None] + likelihood[None, j, :, i] + T1[:, None, :, i-1], dim=0)
    #     mps[:,-1] = torch.argmax(T1[:,:,-1], dim=0)
    #     for i in range(self.t-1, 0, -1):
    #         for j in range(self.n):
    #             idx = mps[j, i].long()
    #             mps[j, i-1] = T2[idx, j, i]
    #
    #     return mps


    def viterbi(self, likelihood, params):
        '''
        apply the viterbi algorithm to find the most probable sequence per patient
        omega is the maximimum joint probability of the previous data and latent states ; the last value is the joint
        distribution of the most probable path
        :return:
        '''
        omega = torch.zeros(self.k, self.n, self.t).to(self.device)
        psi = torch.zeros(self.k, self.n, self.t).to(self.device)
        mps = torch.zeros(self.n, self.t).to(self.device)

        logA = params['A'].log()
        pi = params['pi']

        omega[:, :, 0] = pi[:, None].log() + likelihood[:, :, 0]
        for i in range(1, self.t):

            # omega[:, :, i], psi[:, :, i] = torch.max(likelihood[None, :, :, i] + self.logA[:, :, None] +
            #                                          omega[:, None, :, i-1], dim=0)
            inner_max, psi[:, :, i] = torch.max(logA[:, :, None] + omega[:, None, :, i-1], dim=0)
            omega[:, :, i] = likelihood[:, :, i] + inner_max

        mps[:, -1] = torch.argmax(omega[:, :, -1], dim=0)
        val, _ = torch.max(omega[:, :, -1], dim=0)
        for i in range(self.t-2, -1, -1):
            psi_sample = psi[:, :, i+1]
            mps[:, i] = torch.gather(psi_sample, 0, mps[:, i+1].long().unsqueeze(0))
        return mps, omega, psi

    def change_data(self, data, ins=None, OM=None, TM=None, reset_VI=True, params=[]):
        '''
        Replace model dataset
        :return: none, updates to model params only
        '''

        self.data = data.to(self.device)

        self.n = data.shape[0]
        self.t = data.shape[1]

        # store the inputs used in analysis
        if self.io:
            self.ins = ins.to(self.device)  # n x t x 1

        # store the time mask
        if TM is None:
            self.tm = torch.ones(self.n, self.t, requires_grad=False, device=self.device)
        else:
            self.tm = TM.to(self.device)  # n x t

        # store the observation mask
        if OM is None:
            self.om = torch.ones(self.n, self.t, requires_grad=False, device=self.device)
        else:
            self.om = OM.to(self.device)  # n x t

        if reset_VI:
            if self.perso_io and self.perso:
                self.elbo = []
                mnoise = params['mnoise']
                mi_numpy = np.sqrt(mnoise.cpu().numpy())*np.random.randn(self.n, self.d)
                if self.device[:4] == 'cuda':
                    self.mu_hat = torch.from_numpy(mi_numpy).float().cuda().to(self.device).requires_grad_()
                else:
                    self.mu_hat = torch.from_numpy(mi_numpy).float().requires_grad_()
                if self.VI_diag:
                    self.tril_vec = torch.tensor(0.01 * np.random.randn(self.n * self.d), requires_grad=True,
                                                 device=self.device, dtype=torch.float32)
                    self.L_hat = torch.zeros(self.n, self.d, self.d, device=self.device)
                    self.L_hat[torch.stack([torch.eye(self.d) for _ in range(self.n)]) == 1] = self.tril_vec
                else:
                    self.tril_vec = torch.tensor(0.01*np.random.randn(self.n * int(0.5 * self.d * (self.d + 1))),
                                                requires_grad=True, device=self.device, dtype=torch.float32)
                    self.L_hat = torch.zeros(self.n, self.d, self.d, device=self.device)
                    self.L_hat[torch.tril(torch.ones(self.n, self.d, self.d)) == 1] = self.tril_vec


                nnoise = params['nnoise']
                ni_numpy = np.sqrt(nnoise.cpu().numpy())*np.random.randn(self.n, self.d)
                if self.device[:4] == 'cuda':
                    self.nu_hat = torch.from_numpy(ni_numpy).float().cuda().to(self.device).requires_grad_()
                else:
                    self.nu_hat = torch.from_numpy(ni_numpy).float().requires_grad_()
                if self.VI_diag:
                    self.tril = torch.tensor(0.01 * np.random.randn(self.n * self.d), device=self.device,
                                             requires_grad=True, dtype=torch.float32)
                    self.N_hat = torch.zeros(self.n, self.d, self.d, device=self.device)
                    self.N_hat[torch.stack([torch.eye(self.d) for _ in range(self.n)]) == 1] = self.tril
                else:
                    self.tril = torch.tensor(0.01*np.random.randn(self.n * int(0.5 * self.d * (self.d + 1))),
                                            requires_grad=True, device=self.device, dtype=torch.float32)
                    self.N_hat = torch.zeros(self.n, self.d, self.d, device=self.device)
                    self.N_hat[torch.tril(torch.ones(self.n, self.d, self.d)) == 1] = self.tril


                self.optimizer = torch.optim.Adam([self.mu_hat, self.tril_vec, self.nu_hat, self.tril], lr=0.001)
            elif self.perso_io:
                self.elbo = []
                mnoise = params['mnoise']
                mi_numpy = np.sqrt(mnoise.cpu().numpy()) * np.random.randn(self.n, self.d)
                if self.device[:4] == 'cuda':
                    self.mu_hat = torch.from_numpy(mi_numpy).float().cuda().to(self.device).requires_grad_()
                else:
                    self.mu_hat = torch.from_numpy(mi_numpy).float().requires_grad_()
                if self.VI_diag:
                    self.tril_vec = torch.tensor(0.01 * np.random.randn(self.n * self.d), requires_grad=True,
                                                 device=self.device, dtype=torch.float32)
                    self.L_hat = torch.zeros(self.n, self.d, self.d, device=self.device)
                    self.L_hat[torch.stack([torch.eye(self.d) for _ in range(self.n)]) == 1] = self.tril_vec
                else:
                    self.tril_vec = torch.tensor(0.01*np.random.randn(self.n * int(0.5 * self.d * (self.d + 1))),
                                                requires_grad=True, device=self.device, dtype=torch.float32)
                    self.L_hat = torch.zeros(self.n, self.d, self.d, device=self.device)
                    self.L_hat[torch.tril(torch.ones(self.n, self.d, self.d)) == 1] = self.tril_vec

                self.optimizer = torch.optim.Adam([self.mu_hat, self.tril_vec], lr=0.001)
            elif self.perso:
                self.elbo = []
                nnoise = params['nnoise']
                ni_numpy = np.sqrt(nnoise.cpu().numpy()) * np.random.randn(self.n, self.d)
                if self.device[:4] == 'cuda':
                    self.nu_hat = torch.from_numpy(ni_numpy).float().cuda().to(self.device).requires_grad_()
                else:
                    self.nu_hat = torch.from_numpy(ni_numpy).float().requires_grad_()
                if self.VI_diag:
                    self.tril = torch.tensor(0.01 * np.random.randn(self.n * self.d), device=self.device,
                                             requires_grad=True, dtype=torch.float32)
                    self.N_hat = torch.zeros(self.n, self.d, self.d, device=self.device)
                    self.N_hat[torch.stack([torch.eye(self.d) for _ in range(self.n)]) == 1] = self.tril
                else:
                    self.tril = torch.tensor(0.01*np.random.randn(self.n * int(0.5 * self.d * (self.d + 1))),
                                            requires_grad=True, device=self.device, dtype=torch.float32)
                    self.N_hat = torch.zeros(self.n, self.d, self.d, device=self.device)
                    self.N_hat[torch.tril(torch.ones(self.n, self.d, self.d)) == 1] = self.tril

                self.optimizer = torch.optim.Adam([self.nu_hat, self.tril], lr=0.001)


    def forward_pred(self, params, m_sample=None, n_sample=None):
        '''

        :return:
        '''
        pi = params['pi']
        A = params['A']


        likelihood = self.get_likelihoods(params, m_sample=m_sample, n_sample=n_sample)
        alpha, scaling_factor = self.forward(likelihood, params)
        print(alpha.shape)
        osapd = torch.zeros(self.k, self.n, self.t+1).to(self.device) #one-step-ahead predictive density

        #there is no data yet at t=0, use pi
        osapd[:, :, 0] = pi[:, None].log()
        osapd[:, :, 1:] = torch.logsumexp(A[:, :, None, None].log() + alpha[:, None, :, :], dim=0)

        bs = likelihood + osapd #belief state
        lpe = torch.logsumexp(bs, dim=0) #log probability evidence
        bs = (bs - lpe[None, :, :]).exp()

        osapd = osapd.exp() #return values in probability space
        return osapd, bs, lpe

    def forward_sample(self, prob, ns=100):
        '''
        prob: k x n x t 'one-step-ahead predictive density' p(z_it=j | x_i1, ... x_it-1)
        '''
        vals = torch.zeros(ns, self.n, self.t-1, self.d)
        for i in range(self.t-1):
            for j in range(self.n):
                m = torch.distributions.categorical.Categorical(prob[:, j, i])
                for k in range(ns):
                    draw = m.sample()
                    mvn = torch.distributions.multivariate_normal.MultivariateNormal(self.mu[draw, :] +
                                                                                     self.V[draw, :]*self.ins[j, i+1] +
                                                                                     self.mu_hat[j, :]*self.ins[j, i+1],
                                                                                     covariance_matrix=self.var[draw, :, :] +
                                                                                     torch.mm(self.L_hat[j, :, :], self.L_hat[j, :, :].t())*self.ins[j, i+1])
                    vals[k, j, i, :] = mvn.sample()

        return vals

    def get_beliefstate(self, params, m_sample=None, n_sample=None):
        likelihood = self.get_likelihoods(params, m_sample=m_sample, n_sample=n_sample)
        alpha, scaling_factor = self.forward(likelihood, params)

        return alpha

    def load_model(self, filename, cpu=True):
        '''

        :param filename:
        :return:
        '''
        if cpu:
            trained_model = torch.load(filename, map_location=torch.device('cpu'))
        else:
            trained_model = torch.load(filename)

        ### IMPORTANT ####
        # Note that this is currently not setup to continue training. tril_vec needs to be populated and have
        # requires_grad = True to be able to continue training; this function is only to load in a model. Additional
        # functionality is required to continue training
        self.mu_hat = trained_model['Mi'].to(self.device)
        self.L_hat = trained_model['Li'].to(self.device)

    def baseline_risk(self, params, ns=500, type='sample', m_sample=None):
        '''
        Determine probabilities of state assignment at 1- and 2-years from baseline; sample the observed data using
        those probabilities
        :return:
        '''

        #unpack params
        pi = params['pi']
        A = params['A']
        mu = params['mu']
        var = params['var']
        if self.io:
            V = params['V']
            mnoise = params['mnoise']

        if type=='sample':

            sample_1year = torch.zeros(ns, self.n, self.d)
            sample_2year = torch.zeros(ns, self.n, self.d)

            for k in range(ns):
                if self.perso_io:
                    if m_sample is None:
                        m_sample = mnoise.sqrt() * torch.randn(size=(self.n, self.d))
                likelihood = self.get_likelihoods(params, log=False, m_sample=m_sample) # k x n x t
                #NB: one of the pi elements is zero so we don't work directly in the log space
                p_z1 = pi[:, None]*likelihood[:, :, 0].squeeze() / \
                    (pi[:, None]*likelihood[:, :, 0].squeeze()).sum(0)
                #print('Check sum:', p_z1.sum(0))
                p_z6month = (A[:, :, None]*(A[:, :, None]*p_z1[:, None, :]).sum(0)).sum(0)

                p_z1year = p_z1
                for i in range(4):
                    p_z1year = (A[:, :, None]*p_z1year[:, None, :]).sum(0)

                p_z2year = p_z1year
                for i in range(4):
                    p_z2year = (A[:, :, None]*p_z2year[:, None, :]).sum(0)


                for j in range(self.n):
                    # model seems to be running into an underflow issue for highly progressed patients... hack for now
                    if np.isnan(p_z1year[:,j].detach().numpy()).all():
                        p_z1year[:,j ] = torch.zeros(self.k)
                        p_z1year[-1,j] = 1
                        p_z2year[:, j] = p_z1year[:, j]
                    m1 = torch.distributions.categorical.Categorical(p_z1year[:, j])
                    m2 = torch.distributions.categorical.Categorical(p_z2year[:, j])

                    draw = m1.sample()

                    if self.io:
                        mvn = torch.distributions.multivariate_normal.MultivariateNormal(mu[draw, :] + (V[draw, :] + m_sample[j, :])*self.ins[j, 4],
                                                                                     covariance_matrix=var[draw, :, :])
                    else:
                        mvn = torch.distributions.multivariate_normal.MultivariateNormal(mu[draw, :], covariance_matrix=var[draw, :, :])
                    sample_1year[k, j, :] = mvn.sample()

                    draw = m2.sample()
                    if self.io:
                        mvn = torch.distributions.multivariate_normal.MultivariateNormal(mu[draw, :] + (V[draw, :] + m_sample[j, :])*self.ins[j, 8],
                                                                                     covariance_matrix=var[draw, :, :])
                    else:
                        mvn = torch.distributions.multivariate_normal.MultivariateNormal(mu[draw, :],covariance_matrix=var[draw, :, :])
                    sample_2year[k, j, :] = mvn.sample()
        elif type == 'mean':

            sample_1year = torch.zeros(self.n, self.d)
            sample_2year = torch.zeros(self.n, self.d)

            if self.perso_io:
                m_sample = mnoise.sqrt() * torch.randn(size=(self.n, self.d))
            likelihood = self.get_likelihoods(params, log=False, m_sample=m_sample)  # k x n x t
            # NB: one of the pi elements is zero so we don't work directly in the log space
            p_z1 = pi[:, None] * likelihood[:, :, 0].squeeze() / \
                   (pi[:, None] * likelihood[:, :, 0].squeeze()).sum(0)
            # print('Check sum:', p_z1.sum(0))
            p_z6month = (A[:, :, None] * (A[:, :, None] * p_z1[:, None, :]).sum(0)).sum(0)

            p_z1year = p_z1
            for i in range(4):
                p_z1year = (A[:, :, None] * p_z1year[:, None, :]).sum(0)

            p_z2year = p_z1year
            for i in range(4):
                p_z2year = (A[:, :, None] * p_z2year[:, None, :]).sum(0)

            meds1 = self.ins[:, 4]
            meds2 = self.ins[:, 8]

            sample_1year = torch.einsum('ij, ik->jk',[p_z1year, mu]) + torch.einsum('ij, ik->jk', [p_z1year, V])*meds1[:, None]
            sample_2year = torch.einsum('ij, ik->jk', [p_z2year, mu]) + torch.einsum('ij, ik->jk', [p_z2year,V])*meds2[:, None]


        else:
            sample_1year = torch.zeros(self.n, self.d)
            sample_2year = torch.zeros(self.n, self.d)

            if self.perso_io:
                m_sample = mnoise.sqrt() * torch.randn(size=(self.n, self.d))
            likelihood = self.get_likelihoods(params, log=False, m_sample=m_sample)  # k x n x t
            # NB: one of the pi elements is zero so we don't work directly in the log space
            p_z1 = pi[:, None] * likelihood[:, :, 0].squeeze() / \
                   (pi[:, None] * likelihood[:, :, 0].squeeze()).sum(0)
            # print('Check sum:', p_z1.sum(0))
            p_z6month = (A[:, :, None] * (A[:, :, None] * p_z1[:, None, :]).sum(0)).sum(0)

            p_z1year = p_z1
            for i in range(4):
                p_z1year = (A[:, :, None] * p_z1year[:, None, :]).sum(0)

            p_z2year = p_z1year
            for i in range(4):
                p_z2year = (A[:, :, None] * p_z2year[:, None, :]).sum(0)

            idx1 = torch.argmax(p_z1year, dim=0)
            idx2 = torch.argmax(p_z2year, dim=0)

            for j in range(self.n):
                # model seems to be running into an underflow issue for highly progressed patients... hack for now
                if np.isnan(p_z1year[:, j]).all():
                    p_z1year[:, j] = torch.zeros(self.k)
                    p_z1year[-1, j] = 1
                    p_z2year[:, j] = p_z1year[:, j]

                sample_1year[j, :] = mu[idx1[j]] + V[idx1[j]]*self.ins[j,4]
                sample_2year[j, :] = mu[idx2[j]] + V[idx2[j]]*self.ins[j,8]

        return p_z1year, p_z2year, sample_1year, sample_2year, p_z1, p_z6month


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", default=0)
    parser.add_argument("--n", default=100)
    parser.add_argument("--t", default=10)
    parser.add_argument("--useSyn", default=False)
    parser.add_argument("--filename", default=None)
    parser.add_argument("--k", default=3)
    parser.add_argument("--i", default=1)
    parser.add_argument("--perso", default=False)
    parser.add_argument("--perso_io", default=False)
    parser.add_argument("--io", default=False)
    parser.add_argument("--device", default='cpu')
    args = parser.parse_args()

    np.random.seed(int(args.seed))
    torch.manual_seed(int(args.seed))

    def make_data(n, d, t, k=3, io=False, perso_io=False, perso=False):

        #A = torch.Tensor([[0.25, 0.5, 0.25], [0.6, 0.25, 0.15], [0.3, 0.3, 0.4]])
        #pi = torch.Tensor([0.6, 0.3, 0.1])
        conc = 0.5*torch.ones(k)
        A = torch.zeros(k,k)
        a_dist = torch.distributions.dirichlet.Dirichlet(conc)
        for i in range(k):
            A[i, :] = a_dist.sample()
        pi = a_dist.sample()

        mu = torch.randn(k, d)
        var = torch.rand(k, d)
        #mu = torch.Tensor([[2.5, 2.5], [5, 5], [8, 1]])
        #var = torch.Tensor([[1.2, 0.8], [0.75, 0.75], [0.6, 1.]])
        V = torch.zeros(mu.shape)

        X = torch.zeros(n, t, d)
        Z = torch.zeros(n, t, dtype=torch.long)
        M = torch.zeros(n, d)
        R = torch.zeros(n, d)
        D = torch.zeros(n, t)

        if io:
            #V = torch.Tensor([[0.7, 1.2], [0.2, 0.2], [1.7, 0.6]])
            V = torch.randn(k, 2)
        if perso_io:
            mnoise = 1
            mi_dist = torch.distributions.normal.Normal(torch.zeros(d), mnoise * torch.ones(d))

        if perso:
            rnoise = 0.7
            ri_dist = torch.distributions.normal.Normal(torch.zeros(d), rnoise * torch.ones(d))

        #loop to make data
        for i in range(n):
            if perso_io:
                M[i, :] = mi_dist.sample()
            if perso:
                R[i, :] = ri_dist.sample()

            for j in range(t):
                if j == 0:
                    Z[i, j] = torch.multinomial(pi, num_samples=1).byte()
                    D[i, j] = torch.rand(1)
                    m_dist = torch.distributions.normal.Normal(
                        mu.index_select(0, Z[i, j]) + V.index_select(0, Z[i, j])*D[i,j] + M[i, :]*D[i, j] + R[i, :],
                        var.index_select(0, Z[i, j]))
                    X[i, j, :] = m_dist.sample()

                else:
                    Z[i, j] = torch.multinomial(A[Z[i,j-1],:], num_samples=1)
                    D[i, j] = torch.rand(1)
                    m_dist = torch.distributions.normal.Normal(
                        mu.index_select(0, Z[i, j]) + V.index_select(0, Z[i, j])*D[i,j] + M[i, :]*D[i, j] + R[i, :],
                        var.index_select(0, Z[i, j]))
                    X[i, j, :] = m_dist.sample()

        if perso_io and perso:
            params = {'A': A, 'pi': pi, 'mu': mu, 'var': var, 'V': V, 'mnoise': mnoise, 'rnoise':rnoise}
        elif perso_io:
            params = {'A': A, 'pi': pi, 'mu': mu, 'var': var, 'V': V, 'mnoise': mnoise}
        elif perso:
            params = {'A': A, 'pi': pi, 'mu': mu, 'var': var, 'V': V, 'rnoise': rnoise}
        else:
            params = {'A': A, 'pi': pi, 'mu': mu, 'var': var, 'V': V}

        return X, Z, D, M, R, params

    if not args.useSyn:
        CV_data = joblib.load(filename=args.filename)

        dataset_train = CV_data['trainData']
        dataset_valid = CV_data['validData']

        drugs_train = CV_data['trainDrugs']
        drugs_valid = CV_data['validDrugs']

        k = int(args.k)
        TM_train = CV_data['TM_train']
        OM_train = CV_data['OM_train']
        TM_valid = CV_data['TM_valid']
        OM_valid = CV_data['OM_valid']

        model = HMM(dataset_train, drugs_train, k, TM_train, OM_train, full_cov=True, device=args.device, regV=True)
        mu, var, pi, A, V, mu_hat, L_hat, M0_hat, mnoise_hat, ll, elbo, g, xi = model.learn_model(num_iter=5000, cc=1e-6)
        store_train_ll, train_elbo = model.calc_pX()

        trained_model = {'mu': mu, 'var': var, 'pi': pi, 'A': A, 'Mi': mu_hat, 'gamma': g, 'xi': xi, 'L': L_hat,
                         'M0': M0_hat, 'mnoise': mnoise_hat, 'V': V}
        torch.save(trained_model, './iohmm_v2_pkl/PD_HMM_Model_' + str(int(args.i)) + '_k' + str(k) + '.pkl')

        model.change_data(dataset_valid, drugs_valid, OM_valid, TM_valid)
        store_valid_ll, valid_elbo = model.calc_pX()

        CV_results = {'train': store_train_ll, 'valid': store_valid_ll, 'elbo': elbo, 'validation_elbo': valid_elbo}
        torch.save(CV_results, './iohmm_v2_pkl/PD_HMM_CV_pX_fullTime_v2_CV' + str(int(args.i)) + 'k' + str(k) + '.pkl')

    else:
        t = int(args.t)
        d = 2
        n=int(args.n)
        nv = 50

        #make a training dataset
        X, Z, D, M, R, true_params = make_data(n, d, t, k=int(args.k), perso=False, perso_io=args.perso_io, io=args.io)
        #make a test dataset
        X_test, Z_test, D_test, M_test, R_test, _ = make_data(nv, d, t, k=int(args.k), perso=args.perso, perso_io=args.perso_io, io=args.io)


        time_mask = torch.ones(n,t)
        obs_mask = np.ones((n, t))

        time_mask_valid = torch.ones(nv,t)
        obs_mask_valid = np.ones((nv,t))
        OM_valid = torch.Tensor(obs_mask_valid).float()

        r = np.random.rand(n,t)
        #obs_mask[r < 0.2] = 0
        obs_mask[r < 0] = 0
        OM = torch.Tensor(obs_mask).float()

        niter = 2000

        print(M)
        print(R)
        print(true_params['V'])
        model = HMM(X, D, k=int(args.k), TM=time_mask, OM=OM, full_cov=True, priorV=False, io=args.io, personalized=args.perso,
                    personalized_io=args.perso_io, state_io=args.io)
        if (args.perso and args.perso_io):
            print('running double-personalized IO-HMM')
            params, e_out, ll, elbo, M_hat, L_hat, R_hat, N_hat = model.learn_model(num_iter=3500, cc=1e-3)
        elif args.perso_io:
            print('running med-personalized IO-HMM')
            params, e_out, ll, elbo, M_hat, L_hat = model.learn_model(num_iter=2000, cc=1e-3)
        elif args.perso:
            print('running personalized IO-HMM')
            params, e_out, ll, elbo, R_hat, N_hat = model.learn_model(num_iter=2000, cc=1e-3)
        elif args.io:
            print('running IO-HMM')
            params, e_out, ll = model.learn_model(num_iter=3500, cc=1e-3, intermediate_save=False, use_cc=True)
        else:
            print('running HMM')
            params, e_out, ll = model.learn_model(num_iter=3500, cc=1e-6, intermediate_save=False, use_cc=True)

        mps, omega, psi = model.predict_sequence(params)
        #model.change_data(X_valid, D_valid, time_mask_valid, OM_valid)
        #valid_elbo, valid_elbo_seq, px_seq, M_hat_valid, L_hat_valid = model.calc_pX(niter, returnVars=True)
        #mps_valid = model.predict_sequence()

        #use learn state means to calculate match
        C = np.zeros((int(args.k), int(args.k)))
        for i in range(int(args.k)):
            for j in range(int(args.k)):
                C[i,j] = np.linalg.norm(true_params['mu'][i, :] - params['mu'][j, :])

        import scipy
        row_ind, col_ind = scipy.optimize.linear_sum_assignment(C)
        print(row_ind)
        print(col_ind)

        plt.figure()
        plt.subplot(1,2,1)
        plt.plot(ll, 'o-')
        plt.title('Likelihood')

        plt.subplot(1,2,2)
        plt.plot(np.diff(ll),'o-')
        plt.title('Likelihood Change')

        if args.perso or args.perso_io:
            plt.figure()
            plt.subplot(1,2,1)
            plt.plot(elbo, 'o-')
            plt.title('ELBO')

            plt.subplot(1,2,2)
            plt.plot(np.diff(elbo),'o-')
            plt.title('ELBO Change')

        obs_pi = torch.zeros(3)
        Z_init = Z[:,0]
        c = 0.
        for i in range(3):
            obs_pi[i] = (Z_init.data == c).sum()
            c += 1.

        print('Obs pi:', true_params['pi'])
        print('Est pi:', params['pi'][col_ind])

        plt.figure()
        plt.subplot(1,2,1)
        sns.heatmap(true_params['mu'], annot=True)
        plt.title('True mu')

        plt.subplot(1,2,2)
        sns.heatmap(params['mu'][col_ind, :], annot=True)

        plt.figure()
        plt.subplot(1, 2, 1)
        sns.heatmap(true_params['pi'][np.newaxis, :], annot=True)
        plt.title('True pi')

        plt.subplot(1, 2, 2)
        sns.heatmap(params['pi'][col_ind][np.newaxis, :], annot=True)

        reord_A = np.zeros((int(args.k), int(args.k)))
        for i in range(int(args.k)):
            for j in range(int(args.k)):
                reord_A[i,j] = params['A'][col_ind[i], col_ind[j]]

        plt.figure()
        plt.subplot(1,2,1)
        sns.heatmap(true_params['A'], annot=True)

        plt.subplot(1,2,2)
        sns.heatmap(reord_A, annot=True)



        print('Est A:', params['A'])
        if args.io:
            #print('Est V:', params['V'])

            plt.figure()
            plt.subplot(1, 2, 1)
            sns.heatmap(true_params['V'], annot=True)
            plt.title('True V')

            plt.subplot(1, 2, 2)
            sns.heatmap(params['V'][col_ind, :], annot=True)

        if args.perso_io:
            print('Learned noise: ', params['mnoise'])

            plt.figure()
            plt.subplot(1, 2, 1)
            plt.scatter(M[:, 0], M_hat.detach().numpy()[:, 0])
            plt.plot([-2, 2], [-2, 2], 'k')
            plt.xlabel('M')
            plt.ylabel('M_hat')

            plt.subplot(1, 2, 2)
            plt.scatter(M[:, 1], M_hat.detach().numpy()[:, 1])
            plt.plot([-2, 2], [-2, 2], 'k')
            plt.xlabel('M')
            plt.ylabel('M_hat')
            plt.tight_layout()


        plt.figure()
        plt.subplot(1,2,1)
        plt.imshow(mps)
        plt.title('Most Probable Sequence')

        plt.subplot(1,2,2)
        plt.imshow(Z)
        plt.title('True Z')

        plt.figure()
        plt.subplot(1,2,1)
        plt.scatter(Z[:,0],mps[:, 0] + 0.05*torch.randn(int(args.n)))
        plt.title('Check first obs')

        t_id = np.random.randint(0,t)
        plt.subplot(1,2,2)
        plt.scatter(Z[:,t_id], mps[:,t_id] + 0.05*torch.randn(int(args.n)))

        check_id = np.random.randint(0,n)
        plt.figure()
        plt.scatter(Z[check_id,:], mps[check_id,:])

        plt.show()

        # plt.figure()
        # plt.subplot(1,2,1)
        # plt.scatter(M_valid[:,0], M_hat_valid.detach().numpy()[:, 0])
        # plt.plot([-2, 2], [-2, 2], 'k')
        # plt.xlabel('M_valid')
        # plt.ylabel('M_hat_valid')
        #
        # plt.subplot(1,2,2)
        # plt.scatter(M_valid[:, 1], M_hat_valid.detach().numpy()[:, 1])
        # plt.plot([-2, 2], [-2, 2], 'k')
        # plt.xlabel('M_valid')
        # plt.ylabel('M_hat_valid')

        true_delta = np.zeros((n, t, d))
        obs_delta = np.zeros((n, t, d))

        for i in range(n):
            for j in range(t):
                true_delta[i, j, :] = true_params['V'].index_select(0, Z[i, j])*D[i, j] + M[i, :]*D[i, j]
                obs_delta[i, j, :] = params['V'].index_select(0, mps[i,j].long())*D[i,j] + M_hat.detach()[i, :]*D[i, j]

        true_valid_delta = np.zeros((nv, t, d))
        obs_valid_delta = np.zeros((nv, t, d))

        for i in range(nv):
            for j in range(t):
                true_valid_delta[i,j,:] = V.index_select(0, Z_valid[i,j])*D_valid[i,j] + M_valid[i,:]*D_valid[i,j]
                obs_valid_delta[i,j,:] = V_hat.index_select(0, mps_valid[i,j].long())*D_valid[i,j] + M_hat_valid.detach()[i, :]*D_valid[i,j]

        plt.figure()
        plt.subplot(1,2,1)
        plt.scatter(true_delta[:, :, 0], obs_delta[:, :, 0])
        plt.plot([-2, 2], [-2, 2], 'k')
        plt.xlabel('Delta')
        plt.ylabel('Delta_hat')

        plt.subplot(1,2,2)
        plt.scatter(true_delta[:, :, 1], obs_delta[:, :, 1])
        plt.plot([-2, 2], [-2, 2], 'k')
        plt.xlabel('Delta')
        plt.ylabel('Delta_hat')
        plt.tight_layout()

        plt.figure()
        plt.subplot(1,2,1)
        plt.scatter(true_valid_delta[:, :, 0], obs_valid_delta[:, :, 0])
        plt.plot([-2, 2], [-2, 2], 'k')
        plt.xlabel('Delta_valid')
        plt.ylabel('Delta_valid_hat')

        plt.subplot(1,2,2)
        plt.scatter(true_valid_delta[:, :, 1], obs_valid_delta[:, :, 1])
        plt.plot([-2, 2], [-2, 2], 'k')
        plt.xlabel('Delta_valid')
        plt.ylabel('Delta_valid_hat')
        plt.tight_layout()

        plt.show()


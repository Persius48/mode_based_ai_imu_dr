import torch
import time
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
plt.rcParams["legend.loc"] = "upper right"
plt.rcParams['axes.titlesize'] = 'x-large'
plt.rcParams['axes.labelsize'] = 'x-large'
plt.rcParams['legend.fontsize'] = 'x-large'
plt.rcParams['xtick.labelsize'] = 'x-large'
plt.rcParams['ytick.labelsize'] = 'x-large'
from termcolor import cprint
import numpy as np
import os
from torch.utils.tensorboard import SummaryWriter
from torch.utils.data import DataLoader
from src.utils import *

from datetime import datetime
from src.lie_algebra import SO3, CPUSO3
# from utils import pload, pdump, yload, ydump, mkdir, bmv
# from utils import bmtm, bmtv, bmmt
# from datetime import datetime
# from lie_algebra import SO3, CPUSO3

from src.utils_IEKF import IEKF


class LearningBasedProcessing:
    def __init__(self, res_dir, tb_dir, net_class, net_params, address, dt):
        self.res_dir = res_dir
        self.tb_dir = tb_dir
        self.net_class = net_class
        self.net_params = net_params
        self._ready = False
        self.train_params = {}
        self.figsize = (20, 12)
        self.dt = dt  # (s)
        self.address, self.tb_address = self.find_address(address)
        self.iekf = IEKF()
        self.g = torch.Tensor([0, 0, -9.80665])
        if address is None:  # create new address
            pdump(self.net_params, self.address, 'net_params.p')
            ydump(self.net_params, self.address, 'net_params.yaml')
        else:  # pick the network parameters
            self.net_params = pload(self.address, 'net_params.p')
            self.train_params = pload(self.address, 'train_params.p')
            self._ready = True
        self.path_weights = os.path.join(self.address, 'weights.pt')
        self.net = self.net_class(**self.net_params)
        if self._ready:  # fill network parameters
            self.load_weights(self.iekf)

    def find_address(self, address):
        """return path where net and training info are saved"""
        if address == 'last':
            addresses = sorted(os.listdir(self.res_dir))
            tb_address = os.path.join(self.tb_dir, str(len(addresses)))
            address = os.path.join(self.res_dir, addresses[-1])
        elif address is None:
            now = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
            address = os.path.join(self.res_dir, now)
            mkdir(address)
            tb_address = os.path.join(self.tb_dir, now)
        else:
            tb_address = None
        return address, tb_address

    # def load_weights(self):
    #     weights = torch.load(self.path_weights)
    #     self.net.load_state_dict(weights)

    def load_weights(self, iekf):
        """Load the weights for the net and IEKF components from the saved file"""

        # Load the checkpoint from the file
        checkpoint = torch.load(self.path_weights)

        # Load net parameters
        self.net.load_state_dict(checkpoint['net_state_dict'])

        # Load IEKF parameters (for InitProcessCovNet layers)
        iekf.initprocesscov_net.factor_initial_covariance.load_state_dict(checkpoint['init_cov_state_dict'])
        iekf.initprocesscov_net.factor_process_covariance.load_state_dict(checkpoint['process_cov_state_dict'])



    def train(self, dataset_class, dataset_params, train_params):
        """train the neural network. GPU is assumed"""
        self.train_params = train_params
        pdump(self.train_params, self.address, 'train_params.p')
        ydump(self.train_params, self.address, 'train_params.yaml')

        hparams = self.get_hparams(dataset_class, dataset_params, train_params)
        ydump(hparams, self.address, 'hparams.yaml')

        # define datasets
        dataset_train = dataset_class(**dataset_params, mode='train')
        dataset_train.init_train()
        dataset_val = dataset_class(**dataset_params, mode='val')
        dataset_val.init_val()
        # iekf = IEKF()
        # get class
        Optimizer = train_params['optimizer_class']
        Scheduler = train_params['scheduler_class']
        Loss = train_params['loss_class']

        # get parameters
        dataloader_params = train_params['dataloader']
        optimizer_params = train_params['optimizer']
        scheduler_params = train_params['scheduler']
        loss_params = train_params['loss']

        # define optimizer, scheduler and loss
        dataloader = DataLoader(dataset_train, **dataloader_params)
        dataloader_val = DataLoader(dataset_val, **dataloader_params)
        optimizer = Optimizer(self.net.parameters(), **optimizer_params)
        scheduler = Scheduler(optimizer, **scheduler_params)
        criterion = Loss(**loss_params)

        # remaining training parameters
        freq_val = train_params['freq_val']
        n_epochs = train_params['n_epochs']

        # init net w.r.t dataset
        self.net = self.net
        mean_u, std_u = dataset_train.mean_u.cpu(), dataset_train.std_u.cpu()
        self.net.set_normalized_factors(mean_u, std_u)
        
        sample_data = next(iter(dataloader))
        t, us, xs, p_gt, v_gt, ang_gt, name = sample_data
        us_noise = dataset_train.add_noise(us) 
        # start tensorboard writer
        writer = SummaryWriter(self.tb_address)
        writer.add_graph(self.net, us_noise)
        start_time = time.time()
        best_loss = torch.Tensor([float('Inf')])

        #  define some function for seeing evolution of training
        def write(epoch, loss_epoch):
            writer.add_scalar('loss/train', loss_epoch.item(), epoch)
            writer.add_scalar('lr', optimizer.param_groups[0]['lr'], epoch)
            cprint('Train Epoch: {:2d} \tLoss: {:.4f}'.format(
                epoch, loss_epoch.item()), 'green')
            scheduler.step(epoch)

        def write_time(epoch, start_time):
            delta_t = time.time() - start_time
            print("Amount of time spent for epochs " +
                  "{}-{}: {:.1f}s\n".format(epoch - freq_val, epoch, delta_t))
            writer.add_scalar('time_spend', delta_t, epoch)

        def write_val(loss, best_loss):
            if 0.5 * loss <= best_loss:
                msg = 'validation loss decreases! :) '
                msg += '(curr/prev loss {:.4f}/{:.4f})'.format(loss.item(),
                                                               best_loss.item())
                cprint(msg, 'green')
                best_loss = loss
                self.save_net(self.iekf)
            else:
                msg = 'validation loss increases! :( '
                msg += '(curr/prev loss {:.4f}/{:.4f})'.format(loss.item(),
                                                               best_loss.item())
                cprint(msg, 'yellow')
            writer.add_scalar('loss/val', loss.item(), epoch)
            return best_loss

        n_pre_epochs = 4000
        pre_loss_epoch_train = torch.zeros(n_pre_epochs)
        for epoch in range(1, n_pre_epochs + 1):
            loss_epoch = self.pre_loop_train(dataloader, optimizer, criterion)
            pre_loss_epoch_train[epoch-1] = loss_epoch
            write(epoch, loss_epoch)
            scheduler.step(epoch)
            if epoch % 50 == 0:
                # loss = self.loop_val(dataset_val, criterion)
                loss = self.pre_loop_val(dataloader, criterion)
                write_time(epoch, start_time)
                best_loss = write_val(loss, best_loss)
                start_time = time.time()
        mondict = {
            'pre_loss_epoch_train': pre_loss_epoch_train.cpu(),
        }
        pdump(mondict, self.address, 'pre_loss_epoch_train.p')
        # training loop !
        loss_epoch_train = torch.zeros(n_epochs)
        for epoch in range(1, n_epochs + 1):
            loss_epoch = self.loop_train(dataloader, optimizer, criterion, self.iekf)
            loss_epoch_train[epoch-1] = loss_epoch
            write(epoch, loss_epoch)
            scheduler.step(epoch)
            if epoch % freq_val  == 0:   
                # loss = self.loop_val(dataset_val, criterion)
                loss = self.loop_val(dataloader_val, criterion, self.iekf)
                write_time(epoch, start_time)
                best_loss = write_val(loss, best_loss)
                start_time = time.time()
        # training is over !
        mondict = {
            'loss_epoch_train': loss_epoch_train.cpu(),
        }
        pdump(mondict, self.address, 'loss_epoch_train.p')

        fig_loss, axs_loss = plt.subplots(figsize=(16, 9))
        axs_loss.plot(loss_epoch_train)
        axs_loss.set(xlabel='epochs', ylabel='$\mathbf{loss_epoch_train}_n$', title="loss_epoch_train")
        axs_loss.grid()
        axs_loss.legend(['loss_epoch_train'])
        fig_name = "loss_epoch_train"
        fig_loss.savefig(os.path.join(self.address, fig_name + '.png'))
        fig_loss.clf()
        plt.close()
        writer.close()


    def pre_loop_train(self, dataloader, optimizer, criterion):
        """Forward-backward loop over training data"""
        loss_epoch = 0
        optimizer.zero_grad()

        # iekf = IEKF()
        for t, us, xs, p_gt, v_gt, ang_gt, name in dataloader:
            t, us, xs, p_gt, v_gt, ang_gt = t, us, xs, p_gt, v_gt, ang_gt
            us_noise = dataloader.dataset.add_noise(us)

            # IEKF
            time_net = time.time()
            ys = self.net(us_noise)
            # print(name, "train_time_net = ", "{:.3f}s".format(time.time() - time_net))

            ys_mean = ys.mean(dim=0, keepdim=False).mean(dim=0, keepdim=False)
            ys_A = ys_mean[:6]
            ys_b = ys_mean[6:12]
            ys_mc = ys_mean[12:14]
            print('A =', ys_A)
            print('bias =', ys_b)
            print('mescov =', ys_mc)
            time_IEKF = time.time()
            us_fix = ys[:, :, :6] * us_noise[:, :, :6] - ys[:, :, 6:12]
            # iekf.set_Q()
            measurements_covs = ys[:, :, 12:14]
            # Rot, v, p, b_omega, b_acc, Rot_c_i, t_c_i = \
            #     iekf.run(t, us_fix, measurements_covs, v_gt, p_gt, t.shape[1], ang_gt[:, 0, :])
            # print(name, "train_time_IEKF = ", "{:.3f}s".format(time.time() - time_IEKF))
            time_Loss = time.time()
            # hat_dRot_ij = bbmtm(Rot[:, :-1].clone(), Rot[:, 1:].clone()).double()

            hat_dxi_ij = torch.zeros(us_fix.shape[0], us_fix.shape[1]-1, 3).double()
            Rot_gt = torch.zeros(us_fix.shape[0], us_fix.shape[1], 3, 3).double()
            # hat_acc = torch.zeros(Rot_gt.shape[0], Rot_gt.shape[1], 3).double()

            for i in range(0, Rot_gt.shape[0]):
                # hat_dxi_ij[i] = SO3.log(hat_dRot_ij[i].clone()).double()
                Rot_gt[i] = SO3.from_rpy(ang_gt[i, :, 0], ang_gt[i, :, 1], ang_gt[i, :, 2])

            hat_dRot_gt_ij = bbmtm(Rot_gt[:, :-1].clone(), Rot_gt[:, 1:].clone()).double()
            for i in range(0, Rot_gt.shape[0]):
                hat_dxi_ij[i] = SO3.log(hat_dRot_gt_ij[i].clone()).double()

            hat_acc = (bbmv(Rot_gt[:, :-1], us_fix[:, :-1, 3:6]) + self.g).double()

            hat_dv_ij = (v_gt[:, 1:, :].clone() - v_gt[:, :-1, :].clone()).double()
            hat_dp_ij = (p_gt[:, 1:, :].clone() - p_gt[:, :-1, :].clone()).double()

            # hat_dv_ij = (v[:, 1:, :].clone() - v[:, :-1, :].clone()).double()
            # hat_dp_ij = (p[:, 1:, :].clone() - p[:, :-1, :].clone()).double()

            # hat_xs = torch.cat((hat_dxi_ij.clone(), hat_dv_ij.clone(), hat_dp_ij.clone()), dim=2)
            dt = t[:, 1:] - t[:, :-1]
            hat_xi = torch.einsum('bij, bi -> bij', (us_fix[:, :-1, :3].clone()), dt).double()
            hat_dv = torch.einsum('bij, bi -> bij', (hat_acc.clone()), dt).double()

            # hat_xs = torch.cat((hat_xi.clone(), hat_dv.clone(), hat_dp_ij.clone()), dim=2)

            hat_xs = torch.cat((hat_xi.clone(), hat_dv.clone(),
                                hat_dxi_ij.clone(), hat_dv_ij.clone(), hat_dp_ij.clone()), dim=2)

            loss = criterion(xs[:, :-1, :], hat_xs) / len(dataloader)

            loss = loss.cuda()
            loss.backward()
            loss = loss.cpu()

            loss_epoch += loss.detach().cpu()
            # print(name, "train_time_Loss = ", "{:.3f}s".format(time.time() - time_Loss))

            # with torch.autograd.detect_anomaly():

        optimizer.step()
        return loss_epoch

    def pre_loop_val(self, dataloader, criterion):
        """Forward loop over validation data"""
        loss_epoch = 0
        self.net.eval()
        # iekf = IEKF()
        with torch.no_grad():
            for t, us, xs, p_gt, v_gt, ang_gt, name in dataloader:
                t, us, xs, p_gt, v_gt, ang_gt = t, us, xs, p_gt, v_gt, ang_gt
                us_noise = dataloader.dataset.add_noise(us)
                # IEKF
                time_net = time.time()
                ys = self.net(us_noise)
                # print(name, "val_time_net = ", "{:.3f}s".format(time.time() - time_net))
                time_IEKF = time.time()

                us_fix = ys[:, :, :6] * us_noise[:, :, :6] - ys[:, :, 6:12]
                # iekf.set_Q()
                measurements_covs = ys[:, :, 12:14]
                # Rot, v, p, b_omega, b_acc, Rot_c_i, t_c_i = \
                #     iekf.run(t, us_fix, measurements_covs, v_gt, p_gt, t.shape[1], ang_gt[:, 0, :])

                # print(name, "val_time_IEKF = ", "{:.3f}s".format(time.time() - time_IEKF))

                time_Loss = time.time()

                # hat_dRot_ij = bbmtm(Rot[:, :-1].clone(), Rot[:, 1:].clone()).double()
                hat_dxi_ij = torch.zeros(us_fix.shape[0], us_fix.shape[1]-1, 3).double()
                Rot_gt = torch.zeros(us_fix.shape[0], us_fix.shape[1] - 1, 3, 3).double()
                # hat_acc = torch.zeros(Rot_gt.shape[0], Rot_gt.shape[1], 3).double()
                for i in range(0, Rot_gt.shape[0]):
                    # hat_dxi_ij[i] = SO3.log(hat_dRot_ij[i].clone()).double()
                    Rot_gt[i] = SO3.from_rpy(ang_gt[i, :-1, 0], ang_gt[i, :-1, 1], ang_gt[i, :-1, 2])
                hat_acc = (bbmv(Rot_gt, us_fix[:, :-1, 3:6]) + self.g).double()
                hat_dv_ij = (v_gt[:, 1:, :].clone() - v_gt[:, :-1, :].clone()).double()
                hat_dp_ij = (p_gt[:, 1:, :].clone() - p_gt[:, :-1, :].clone()).double()
                # hat_dv_ij = (v[:, 1:, :].clone() - v[:, :-1, :].clone()).double()
                # hat_dp_ij = (p[:, 1:, :].clone() - p[:, :-1, :].clone()).double()
                # hat_xs = torch.cat((hat_dxi_ij.clone(), hat_dv_ij.clone(), hat_dp_ij.clone()), dim=2)
                # hat_xs = torch.cat((us_fix[:, :-1, :3].clone(), hat_acc[:, :, :].clone(), hat_dp_ij.clone()), dim=2)

                dt = t[:, 1:] - t[:, :-1]
                hat_xi = torch.einsum('bij, bi -> bij', (us_fix[:, :-1, :3].clone()), dt).double()
                hat_dv = torch.einsum('bij, bi -> bij', (hat_acc.clone()), dt).double()
                # hat_xs = torch.cat((hat_xi.clone(), hat_dv.clone(), hat_dp_ij.clone()), dim=2)
                hat_xs = torch.cat((hat_xi.clone(), hat_dv.clone(),
                                    hat_dxi_ij.clone(), hat_dv_ij.clone(), hat_dp_ij.clone()), dim=2)

                loss = criterion(xs[:, :-1, :], hat_xs) / len(dataloader)


                loss_epoch += loss.cpu()
                # print(name, "val_time_Loss = ", "{:.3f}s".format(time.time() - time_Loss))

        self.net.train()
        return loss_epoch

    def loop_train(self, dataloader, optimizer, criterion, iekf):
        """Forward-backward loop over training data"""
        loss_epoch = 0
        optimizer.zero_grad()

        for t, us, xs, p_gt, v_gt, ang_gt, name in dataloader:
            t, us, xs, p_gt, v_gt, ang_gt = t, us, xs, p_gt, v_gt, ang_gt
            us_noise = dataloader.dataset.add_noise(us)

            # IEKF
            time_net = time.time()
            ys = self.net(us_noise)
            print(name, "train_time_net = ", "{:.3f}s".format(time.time() - time_net))

            ys_mean = ys.mean(dim=0, keepdim=False).mean(dim=0, keepdim=False)
            ys_A = ys_mean[:6]
            ys_b = ys_mean[6:12]
            ys_mc = ys_mean[12:14]
            print('A =', ys_A)
            print('bias =', ys_b)
            print('mescov =', ys_mc)
            time_IEKF = time.time()
            us_fix = ys[:, :, :6] * us_noise[:, :, :6] - ys[:, :, 6:12]
            iekf.set_Q()
            measurements_covs = ys[:, :, 12:14]

            Rot, v, p, b_omega, b_acc, Rot_c_i, t_c_i = \
                iekf.run(t, us_fix, measurements_covs, v_gt, p_gt, t.shape[1], ang_gt[:, 0, :])

            print(name, "train_time_IEKF = ", "{:.3f}s".format(time.time() - time_IEKF))
            time_Loss = time.time()
            hat_dRot_ij = bbmtm(Rot[:, :-1].clone(), Rot[:, 1:].clone()).double()

            hat_dxi_ij = torch.zeros(hat_dRot_ij.shape[0], hat_dRot_ij.shape[1], 3).double()
            Rot_gt = torch.zeros(us_fix.shape[0], us_fix.shape[1] - 1, 3, 3).double()
            # hat_acc = torch.zeros(Rot_gt.shape[0], Rot_gt.shape[1], 3).double()

            for i in range(0, Rot_gt.shape[0]):
                hat_dxi_ij[i] = SO3.log(hat_dRot_ij[i].clone()).double()
                Rot_gt[i] = SO3.from_rpy(ang_gt[i, :-1, 0], ang_gt[i, :-1, 1], ang_gt[i, :-1, 2])

            hat_acc = (bbmv(Rot_gt, us_fix[:, :-1, 3:6]) + self.g).double()

            # hat_dv_ij = (v_gt[:, 1:, :].clone() - v_gt[:, :-1, :].clone()).double()
            # hat_dp_ij = (p_gt[:, 1:, :].clone() - p_gt[:, :-1, :].clone()).double()

            hat_dv_ij = (v[:, 1:, :].clone() - v[:, :-1, :].clone()).double()
            hat_dp_ij = (p[:, 1:, :].clone() - p[:, :-1, :].clone()).double()

            # hat_xs = torch.cat((hat_dxi_ij.clone(), hat_dv_ij.clone(), hat_dp_ij.clone()), dim=2)
            dt = t[:, 1:] - t[:, :-1]
            hat_xi = torch.einsum('bij, bi -> bij', (us_fix[:, :-1, :3].clone()), dt).double()
            hat_dv = torch.einsum('bij, bi -> bij', (hat_acc.clone()), dt).double()

            # hat_xs = torch.cat((hat_xi.clone(), hat_dv.clone(), hat_dp_ij.clone()), dim=2)

            hat_xs = torch.cat((hat_xi.clone(), hat_dv.clone(),
                                hat_dxi_ij.clone(), hat_dv_ij.clone(), hat_dp_ij.clone()), dim=2)

            # def print_grad(grad):
            #     print("Gradient on model.fc1.weight:\n", grad)
            # iekf.initprocesscov_net.factor_process_covariance.weight.register_hook(print_grad)

            loss = criterion(xs[:, :-1, :], hat_xs) / len(dataloader)

            loss = loss.cuda()
            loss.backward()
            loss = loss.cpu()



            loss_epoch += loss.detach().cpu()
            print(name, "train_time_Loss = ", "{:.3f}s".format(time.time() - time_Loss))

            # with torch.autograd.detect_anomaly():

        optimizer.step()
        return loss_epoch

    def loop_val(self, dataloader, criterion, iekf):
        """Forward loop over validation data"""
        loss_epoch = 0
        self.net.eval()
        # iekf = IEKF()
        with torch.no_grad():
            for t, us, xs, p_gt, v_gt, ang_gt, name in dataloader:
                t, us, xs, p_gt, v_gt, ang_gt = t, us, xs, p_gt, v_gt, ang_gt
                us_noise = dataloader.dataset.add_noise(us)
                # IEKF
                time_net = time.time()
                ys = self.net(us_noise)
                print(name, "val_time_net = ", "{:.3f}s".format(time.time() - time_net))
                time_IEKF = time.time()

                us_fix = ys[:, :, :6] * us_noise[:, :, :6] - ys[:, :, 6:12]
                self.iekf.set_Q()
                measurements_covs = ys[:, :, 12:14]

                Rot, v, p, b_omega, b_acc, Rot_c_i, t_c_i = \
                    iekf.run(t, us_fix, measurements_covs, v_gt, p_gt, t.shape[1], ang_gt[:, 0, :])

                print(name, "val_time_IEKF = ", "{:.3f}s".format(time.time() - time_IEKF))

                time_Loss = time.time()

                hat_dRot_ij = bbmtm(Rot[:, :-1].clone(), Rot[:, 1:].clone()).double()
                hat_dxi_ij = torch.zeros(hat_dRot_ij.shape[0], hat_dRot_ij.shape[1], 3).double()
                Rot_gt = torch.zeros(us_fix.shape[0], us_fix.shape[1] - 1, 3, 3).double()
                # hat_acc = torch.zeros(Rot_gt.shape[0], Rot_gt.shape[1], 3).double()
                for i in range(0, Rot_gt.shape[0]):
                    hat_dxi_ij[i] = SO3.log(hat_dRot_ij[i].clone()).double()
                    Rot_gt[i] = SO3.from_rpy(ang_gt[i, :-1, 0], ang_gt[i, :-1, 1], ang_gt[i, :-1, 2])
                hat_acc = (bbmv(Rot_gt, us_fix[:, :-1, 3:6]) + self.g).double()
                # hat_dv_ij = (v_gt[:, 1:, :].clone() - v_gt[:, :-1, :].clone()).double()
                # hat_dp_ij = (p_gt[:, 1:, :].clone() - p_gt[:, :-1, :].clone()).double()
                hat_dv_ij = (v[:, 1:, :].clone() - v[:, :-1, :].clone()).double()
                hat_dp_ij = (p[:, 1:, :].clone() - p[:, :-1, :].clone()).double()
                # hat_xs = torch.cat((hat_dxi_ij.clone(), hat_dv_ij.clone(), hat_dp_ij.clone()), dim=2)
                # hat_xs = torch.cat((us_fix[:, :-1, :3].clone(), hat_acc[:, :, :].clone(), hat_dp_ij.clone()), dim=2)

                dt = t[:, 1:] - t[:, :-1]
                hat_xi = torch.einsum('bij, bi -> bij', (us_fix[:, :-1, :3].clone()), dt).double()
                hat_dv = torch.einsum('bij, bi -> bij', (hat_acc.clone()), dt).double()
                # hat_xs = torch.cat((hat_xi.clone(), hat_dv.clone(), hat_dp_ij.clone()), dim=2)
                hat_xs = torch.cat((hat_xi.clone(), hat_dv.clone(),
                                    hat_dxi_ij.clone(), hat_dv_ij.clone(), hat_dp_ij.clone()), dim=2)

                loss = criterion(xs[:, :-1, :], hat_xs) / len(dataloader)


                loss_epoch += loss.cpu()
                print(name, "val_time_Loss = ", "{:.3f}s".format(time.time() - time_Loss))

        self.net.train()
        return loss_epoch

    # def save_net(self):
    #     """save the weights on the net in CPU"""
    #     self.net.eval().cpu()
    #     torch.save(self.net.state_dict(), self.path_weights)
    #     self.net.train()

    def save_net(self, iekf):
        """Save the weights for both the net and IEKF components"""

        # Move the net to CPU for saving
        self.net.eval().cpu()

        # Create a dictionary to store the model and IEKF state
        save_dict = {
            'net_state_dict': self.net.state_dict(),
            'init_cov_state_dict': iekf.initprocesscov_net.factor_initial_covariance.state_dict(),
            'process_cov_state_dict': iekf.initprocesscov_net.factor_process_covariance.state_dict()
        }

        # Save the dictionary to the path
        torch.save(save_dict, self.path_weights)

        # Set the net back to training mode
        self.net.train()


    def get_hparams(self, dataset_class, dataset_params, train_params):
        """return all training hyperparameters in a dict"""
        Optimizer = train_params['optimizer_class']
        Scheduler = train_params['scheduler_class']
        Loss = train_params['loss_class']

        # get training class parameters
        dataloader_params = train_params['dataloader']
        optimizer_params = train_params['optimizer']
        scheduler_params = train_params['scheduler']
        loss_params = train_params['loss']

        # remaining training parameters
        freq_val = train_params['freq_val']
        n_epochs = train_params['n_epochs']

        dict_class = {
            'Optimizer': str(Optimizer),
            'Scheduler': str(Scheduler),
            'Loss': str(Loss)
        }

        return {**dict_class, **dataloader_params, **optimizer_params,
                **loss_params, **scheduler_params,
                'n_epochs': n_epochs, 'freq_val': freq_val}

    def test(self, dataset_class, dataset_params, modes, display_only = False):

        Loss = self.train_params['loss_class']
        loss_params = self.train_params['loss']
        criterion = Loss(**loss_params)

        for mode in modes:
            dataset = dataset_class(**dataset_params, mode=mode)
            if display_only:
                self.display_test(dataset, mode)
            else:
                self.loop_test(dataset, criterion, self.iekf)
                self.display_test(dataset, mode)

    def loop_test(self, dataset, criterion, iekf):
        """Forward loop over test data"""
        self.net.eval()
        for i in range(len(dataset)):
            seq = dataset.sequences[i]
            # iekf = IEKF()

            t, us, xs, p_gt, v_gt, ang_gt, name = dataset[i]

            t, us, xs, p_gt, v_gt, ang_gt = \
                t, us, xs, p_gt, v_gt, ang_gt

            t = t.clone().unsqueeze(0)
            us = us.clone().unsqueeze(0)
            xs = xs.clone().unsqueeze(0)
            p_gt = p_gt.clone().unsqueeze(0)
            v_gt = v_gt.clone().unsqueeze(0)
            ang_gt = ang_gt.clone().unsqueeze(0)

            us_noise = dataset.add_noise(us)

            with torch.no_grad():
                # IEKF
                time_net = time.time()
                ys = self.net(us_noise)


                print(name, "test_time_net = ", "{:.3f}s".format(time.time() - time_net))
                time_IEKF = time.time()

                us_fix = ys[:, :, :6] * us_noise[:, :, :6] - ys[:, :, 6:12] #is there a mistake?
                iekf.set_Q()
                measurements_covs = ys[:, :, 12:14]
                Rot, v, p, b_omega, b_acc, Rot_c_i, t_c_i = \
                    iekf.run(t, us_fix, measurements_covs, v_gt, p_gt, t.shape[1], ang_gt[:, 0, :])

                print(name, "test_time_IEKF = ", "{:.3f}s".format(time.time() - time_IEKF))

                hat_dRot_ij = bbmtm(Rot[:, :-1].clone(), Rot[:, 1:].clone()).double()
                hat_dxi_ij = torch.zeros(hat_dRot_ij.shape[0], hat_dRot_ij.shape[1], 3).double()
                Rot_gt = torch.zeros(us_fix.shape[0], us_fix.shape[1] - 1, 3, 3).double()

                for i in range(0, Rot_gt.shape[0]):
                    hat_dxi_ij[i] = SO3.log(hat_dRot_ij[i].clone()).double()
                    Rot_gt[i] = SO3.from_rpy(ang_gt[i, :-1, 0], ang_gt[i, :-1, 1], ang_gt[i, :-1, 2])

                hat_acc = (bbmv(Rot_gt, us_fix[:, :-1, 3:6]) + self.g).double()
                hat_dv_ij = (v[:, 1:, :].clone() - v[:, :-1, :].clone()).double()
                hat_dp_ij = (p[:, 1:, :].clone() - p[:, :-1, :].clone()).double()

                dt = t[:, 1:] - t[:, :-1]
                hat_xi = torch.einsum('bij, bi -> bij', (us_fix[:, :-1, :3].clone()), dt).double()
                hat_dv = torch.einsum('bij, bi -> bij', (hat_acc.clone()), dt).double()

                hat_xs = torch.cat((hat_xi.clone(), hat_dv.clone(),
                                    hat_dxi_ij.clone(), hat_dv_ij.clone(), hat_dp_ij.clone()), dim=2)
                time_dateset = time.time() - time_net
                print('time_dateset=',time_dateset)

            loss = criterion(xs[:, :-1, :], hat_xs)

            print(name, "test_loss = ", "{:.3f}".format(loss))
            mkdir(self.address, seq)
            mondict = {
                'xs': xs[0].cpu(),
                'hat_xs': hat_xs[0].cpu(),
                'loss': loss.cpu().item(),

                't': t[0].cpu(),
                'us': us[0].cpu(),

                'p_gt': p_gt[0].cpu(),
                'ang_gt': ang_gt[0].cpu(),
                'v_gt': v_gt[0].cpu(),

                'ys': ys[0].cpu(),
                'us_fix': us_fix[0].cpu(),
                'us_noise': us_noise[0].cpu(),
                'measurements_covs': measurements_covs[0].cpu(),

                'Rot': Rot[0].cpu(),
                'v': v[0].cpu(),
                'p': p[0].cpu(),
                'b_omega': b_omega[0].cpu(),
                'b_acc': b_acc[0].cpu(),
                'Rot_c_i': Rot_c_i[0].cpu(),
                't_c_i': t_c_i[0].cpu(),
                'time_dateset': time_dateset,
            }
            pdump(mondict, self.address, seq, 'results.p')

    def display_test(self, dataset, mode):
        raise NotImplementedError


class GyroLearningBasedProcessing(LearningBasedProcessing):
    def __init__(self, res_dir, tb_dir, net_class, net_params, address, dt):
        super().__init__(res_dir, tb_dir, net_class, net_params, address, dt)
        # self.roe_dist = [7, 14, 21, 28, 35]  # m
        # self.freq = 100  #  subsampling frequency for RTE computation
        # self.roes = {  # relative trajectory errors
        #     'Rots': [],
        #     'yaws': [],
        # }

    def display_test(self, dataset, mode):
        # self.roes = {
        #     'Rots': [],
        #     'yaws': [],
        # }
        # self.to_open_vins(dataset)
        for i, seq in enumerate(dataset.sequences):
            # print('\n', 'Results for sequence ' + seq)
            # self.seq = seq
            # # get ground truth
            # self.gt = dataset.load_seq(i)
            # Rots = SO3.from_quaternion(self.gt['qs'])
            # self.gt['Rots'] = Rots.cpu()
            # self.gt['rpys'] = SO3.to_rpy(Rots).cpu()
            # # get data and estimate
            # self.net_us = pload(self.address, seq, 'results.p')['hat_xs']
            # self.raw_us, _ = dataset[i]
            # N = self.net_us.shape[0]
            # self.gyro_corrections =  (self.raw_us[:, :3] - self.net_us[:N, :3])
            # self.ts = torch.linspace(0, N*self.dt, N)
            #
            # self.convert()
            # self.plot_gyro()
            # self.plot_gyro_correction()
            # plt.show()

            self.seq = seq
            # self.test_seq = dataset.load_seq(i)
            self.test_result = pload(self.address, seq, 'results.p')

            t = self.test_result['t']
            p_gt = self.test_result['p_gt']
            v_gt = self.test_result['v_gt']
            ang_gt = self.test_result['ang_gt']
            p = self.test_result['p']
            v = self.test_result['v']
            Rot = self.test_result['Rot']

            us = self.test_result["us"]
            ys = self.test_result["ys"]
            us_fix = self.test_result["us_fix"]
            us_noise = self.test_result["us_noise"]
            measurements_covs = self.test_result['measurements_covs']

            hat_xs = self.test_result["hat_xs"]
            xs = self.test_result["xs"]

            b_omega = self.test_result['b_omega']
            b_acc = self.test_result['b_acc']
            Rot_c_i = self.test_result['Rot_c_i']
            t_c_i = self.test_result['t_c_i']

            ang = SO3.to_rpy(Rot)

            time_dateset = self.test_result['time_dateset']

            print(seq, ',', time_dateset)

            # position,
            self.plot_P_3(t, p, p_gt)
            # velocity
            self.plot_V_3(t, v, v_gt)
            # p
            self.plot_P_xy(p, p_gt)
            self.plot_P_x_y_theta_delta(p, p_gt, ang[:, 2], ang_gt[:, 2])
            # self.plot_error_delta(p, p_gt, 100)
            # RPY
            self.plot_RPY(t, ang, ang_gt)
            # # b_omega
            # self.plot_b_omega_3(t, b_omega)
            # # b_omega
            # self.plot_b_acc_3(t, b_acc)

            # # measurements_covs
            # self.plot_measurements_covs(t, measurements_covs)

            # self.plot_ys_b_omega_3(t, ys, us_noise, us)
            # self.plot_ys_b_acc_3(t, ys, us_noise, us)
            self.plot_usfix_us_omega_3(t, us_fix[:, :3], us_noise[:, :3], us[:, :3])
            # self.plot_usfix_us_acc_3(t, us_fix[:, 3:6], us_noise[:, 3:6], us[:, 3:6])
            # self.plot_xs_hatxs_acc_3(t[:-1], xs[:-1, 3:6], hat_xs[:, 3:6])
            plt.show(block=True)



    def convert(self):
        # s -> min
        l = 1 / 60
        self.ts *= l

        # rad -> deg
        l = 180 / np.pi
        self.gyro_corrections *= l
        self.gt['rpys'] *= l

    def integrate_with_quaternions_superfast(self, N, raw_us, net_us):
        imu_qs = SO3.qnorm(SO3.qexp(raw_us[:, :3].double() * self.dt))
        net_qs = SO3.qnorm(SO3.qexp(net_us[:, :3].double() * self.dt))
        Rot0 = SO3.qnorm(self.gt['qs'][:2].double())
        imu_qs[0] = Rot0[0]
        net_qs[0] = Rot0[0]

        N = np.log2(imu_qs.shape[0])
        for i in range(int(N)):
            k = 2 ** i
            imu_qs[k:] = SO3.qnorm(SO3.qmul(imu_qs[:-k], imu_qs[k:]))
            net_qs[k:] = SO3.qnorm(SO3.qmul(net_qs[:-k], net_qs[k:]))

        if int(N) < N:
            k = 2 ** int(N)
            k2 = imu_qs[k:].shape[0]
            imu_qs[k:] = SO3.qnorm(SO3.qmul(imu_qs[:k2], imu_qs[k:]))
            net_qs[k:] = SO3.qnorm(SO3.qmul(net_qs[:k2], net_qs[k:]))

        imu_Rots = SO3.from_quaternion(imu_qs).float()
        net_Rots = SO3.from_quaternion(net_qs).float()
        return net_qs.cpu(), imu_Rots, net_Rots

    def plot_gyro(self):
        N = self.raw_us.shape[0]
        raw_us = self.raw_us[:, :3]
        net_us = self.net_us[:, :3]

        net_qs, imu_Rots, net_Rots = self.integrate_with_quaternions_superfast(N,
                                                                               raw_us, net_us)
        imu_rpys = 180 / np.pi * SO3.to_rpy(imu_Rots).cpu()
        net_rpys = 180 / np.pi * SO3.to_rpy(net_Rots).cpu()
        self.plot_orientation(imu_rpys, net_rpys, N)
        self.plot_orientation_error(imu_Rots, net_Rots, N)

    def plot_orientation(self, imu_rpys, net_rpys, N):
        title = "Orientation estimation"
        gt = self.gt['rpys'][:N]
        fig, axs = plt.subplots(3, 1, sharex=True, figsize=self.figsize)
        axs[0].set(ylabel='roll (deg)', title=title)
        axs[1].set(ylabel='pitch (deg)')
        axs[2].set(xlabel='$t$ (min)', ylabel='yaw (deg)')

        for i in range(3):
            axs[i].plot(self.ts, gt[:, i], color='black', label=r'ground truth')
            axs[i].plot(self.ts, imu_rpys[:, i], color='red', label=r'raw IMU')
            axs[i].plot(self.ts, net_rpys[:, i], color='blue', label=r'net IMU')
            axs[i].set_xlim(self.ts[0], self.ts[-1])
        self.savefig(axs, fig, 'orientation')

    def plot_orientation_error(self, imu_Rots, net_Rots, N):
        gt = self.gt['Rots'][:N]
        raw_err = 180 / np.pi * SO3.log(bmtm(imu_Rots, gt)).cpu()
        net_err = 180 / np.pi * SO3.log(bmtm(net_Rots, gt)).cpu()
        title = "$SO(3)$ orientation error"
        fig, axs = plt.subplots(3, 1, sharex=True, figsize=self.figsize)
        axs[0].set(ylabel='roll (deg)', title=title)
        axs[1].set(ylabel='pitch (deg)')
        axs[2].set(xlabel='$t$ (min)', ylabel='yaw (deg)')

        for i in range(3):
            axs[i].plot(self.ts, raw_err[:, i], color='red', label=r'raw IMU')
            axs[i].plot(self.ts, net_err[:, i], color='blue', label=r'net IMU')
            axs[i].set_ylim(-10, 10)
            axs[i].set_xlim(self.ts[0], self.ts[-1])
        self.savefig(axs, fig, 'orientation_error')

    def plot_gyro_correction(self):
        title = "Gyro correction" + self.end_title
        ylabel = 'gyro correction (deg/s)'
        fig, ax = plt.subplots(figsize=self.figsize)
        ax.set(xlabel='$t$ (min)', ylabel=ylabel, title=title)
        plt.plot(self.ts, self.gyro_corrections, label=r'net IMU')
        ax.set_xlim(self.ts[0], self.ts[-1])
        self.savefig(ax, fig, 'gyro_correction')

    def plot_P_3(self, t, p, p_gt):
        fig1, axs1 = plt.subplots(3, 1, sharex=True, figsize=(16, 9))
        axs1[0].plot(t, p_gt[:, 0])
        axs1[0].plot(t, p[:, 0])
        axs1[1].plot(t, p_gt[:, 1])
        axs1[1].plot(t, p[:, 1])
        axs1[2].plot(t, p_gt[:, 2])
        axs1[2].plot(t, p[:, 2])
        axs1[0].set(xlabel='time (s)', ylabel='$\mathbf{p}_n$ (m)', title="Position X")
        axs1[1].set(xlabel='time (s)', ylabel='$\mathbf{p}_n$ (m)', title="Position Y")
        axs1[2].set(xlabel='time (s)', ylabel='$\mathbf{p}_n$ (m)', title="Position Z")
        axs1[0].grid()
        axs1[1].grid()
        axs1[2].grid()
        axs1[0].legend(['$p_n^x$', '$\hat{p}_n^x$'])
        axs1[1].legend(['$p_n^y$', '$\hat{p}_n^y$'])
        axs1[2].legend(['$p_n^z$', '$\hat{p}_n^z$'])
        fig_name = "p_3"
        fig1.savefig(os.path.join(self.address, self.seq, fig_name + '.png'))
        # fig1.savefig(os.path.join(self.address, self.seq, fig_name + '.eps'), format="eps")
        # plt.show(block=False)
        # plt.pause(2)
        # fig1.clf()
        # plt.close()

    def plot_V_3(self, t, v, v_gt):
        fig2, axs2 = plt.subplots(3, 1, sharex=True, figsize=(16, 9))
        # fig1, axs1 = plt.subplots(figsize=(20, 10))
        axs2[0].plot(t, v_gt[:, 0])
        axs2[0].plot(t, v[:, 0])
        axs2[1].plot(t, v_gt[:, 1])
        axs2[1].plot(t, v[:, 1])
        axs2[2].plot(t, v_gt[:, 2])
        axs2[2].plot(t, v[:, 2])

        axs2[0].set(xlabel='time (s)', ylabel='$\mathbf{v}_n$ (m/s)', title="velocity x")
        axs2[1].set(xlabel='time (s)', ylabel='$\mathbf{v}_n$ (m/s)', title="velocity y")
        axs2[2].set(xlabel='time (s)', ylabel='$\mathbf{v}_n$ (m/s)', title="velocity z")
        axs2[0].grid()
        axs2[1].grid()
        axs2[2].grid()
        axs2[0].legend(['$v_n^x$', '$\hat{v}_n^x$'])
        axs2[1].legend(['$v_n^y$', '$\hat{v}_n^y$'])
        axs2[2].legend(['$v_n^z$', '$\hat{v}_n^z$'])
        fig_name = "v_3"
        fig2.savefig(os.path.join(self.address, self.seq, fig_name + '.png'))
        # fig2.savefig(os.path.join(self.address, self.seq, fig_name + '.eps'), format="eps")
        # plt.show(block=False)
        # plt.pause(2)
        # fig2.clf()
        # plt.close()

    def plot_RPY(self, t, ang, ang_gt):
        fig4, axs4 = plt.subplots(3, 1, sharex=True, figsize=(16, 9))

        axs4[0].plot(t, ang_gt[:, 0])
        axs4[0].plot(t, ang[:, 0])
        axs4[1].plot(t, ang_gt[:, 1])
        axs4[1].plot(t, ang[:, 1])
        axs4[2].plot(t, ang_gt[:, 2])
        axs4[2].plot(t, ang[:, 2])

        axs4[0].set(xlabel='time (s)', ylabel='$\mathbf{\phi}_n$ (rad)', title="Roll")
        axs4[1].set(xlabel='time (s)', ylabel='$\mathbf{\Theta}_n$ (rad)', title="Pitch")
        axs4[2].set(xlabel='time (s)', ylabel='$\mathbf{\psi}_n$ (rad)', title="Yaw")
        axs4[0].grid();
        axs4[1].grid();
        axs4[2].grid()
        axs4[0].legend(['$roll$', '$\hat{roll}_n^x$'])
        axs4[1].legend(['$pitch$', '$\hat{pitch}_n^y$'])
        axs4[2].legend(['$yaw$', '$\hat{yaw}_n^z$'])
        fig_name = "RPY"
        fig4.savefig(os.path.join(self.address, self.seq, fig_name + '.png'))
        # fig4.savefig(os.path.join(self.address, self.seq, fig_name + '.eps'), format="eps")
        # fig4.clf()
        # plt.close()

    def plot_P_xy(self, p, p_gt):
        fig3, ax3 =  plt.subplots(figsize=(12, 10))
        # font_TNR = fm.FontProperties(family='Times New Roman', size=25, stretch=0)
        linewidth = 2
        ax3.plot(p_gt[:, 0], p_gt[:, 1], color='red', linestyle='-.', linewidth=linewidth)
        ax3.plot(p[:, 0], p[:, 1], color='green', linestyle='-', linewidth=linewidth)
        ax3.axis('equal')
        # ax3.set(xlabel=r'$p_n^x$ (m)', ylabel=r'$p_n^y$ (m)')
        ax3.set_xlabel(r'$p_n^x$ (m)')#fontproperties=font_TNR
        ax3.set_ylabel(r'$p_n^y$ (m)')#fontproperties=font_TNR

        ax3.grid()
        ax3.legend(['Ground-Truth', 'Proposed'], loc='upper left', prop={'size': 15}) #'family': 'Times New Roman',
        ax3.tick_params(axis='both', labelsize=24, direction='in')

        fig_name = "p"
        fig3.savefig(os.path.join(self.address, self.seq, fig_name + '.svg'), format='svg', bbox_inches='tight', pad_inches=0.02)
        # fig3.savefig(os.path.join(self.address, self.seq, fig_name + '.eps'), format="eps")
        # fig3.clf()
        # plt.close()

    def plot_P_x_y_theta_delta(self, p, p_gt, yaw, yaw_gt ):
        dp = p[1:, :] - p[:-1, :]  # Delta for the estimated position
        dp_gt = p_gt[1:, :] - p_gt[:-1, :]  # Delta for the ground truth position
        dp_x = dp[:, 0]
        dp_y = dp[:, 1]
        dp_gt_x = dp_gt[:, 0]
        dp_gt_y = dp_gt[:, 1]
        dp_yaw = yaw[1:] - yaw[:-1]
        dp_yaw_gt = yaw_gt[1:] - yaw_gt[:-1]

        fig, axs = plt.subplots(3, 1, figsize=(12, 10), sharex=True)
        # Plot dp_x and dp_gt_x on the first axis
        axs[0].plot(dp_x, label='Δp_x (Estimated)', color='green', linestyle='-', linewidth=2)
        axs[0].plot(dp_gt_x, label='Δp_x (Ground Truth)', color='red', linestyle='--', linewidth=2)
        axs[0].set_ylabel('Δp_x (m)', fontsize=14)
        axs[0].legend(loc='upper right', fontsize=12)
        axs[0].grid(True)
        axs[0].set_title('Delta X Coordinates (Δp_x)', fontsize=16)

         # Plot dp_y and dp_gt_y on the second axis
        axs[1].plot(dp_y, label='Δp_y (Estimated)', color='green', linestyle='-', linewidth=2)
        axs[1].plot(dp_gt_y, label='Δp_y (Ground Truth)', color='red', linestyle='--', linewidth=2)
        axs[1].set_ylabel('Δp_y (m)', fontsize=14)
        axs[1].set_xlabel('Time (Samples)', fontsize=14)
        axs[1].legend(loc='upper right', fontsize=12)
        axs[1].grid(True)
        axs[1].set_title('Delta Y Coordinates (Δp_y)', fontsize=16)

        axs[2].plot(dp_yaw, label='Δyaw (Estimated)', color='green', linestyle='-', linewidth=2)
        axs[2].plot(dp_yaw_gt, label='Δyaw (Ground Truth)', color='red', linestyle='--', linewidth=2)
        axs[2].set_ylabel('Δyaw (rad)', fontsize=14)
        axs[2].set_xlabel('Time (Samples)', fontsize=14)
        axs[2].legend(loc='upper right', fontsize=12)
        axs[2].grid(True)
        axs[2].set_title('Delta yaw (Δyaw)', fontsize=16)
        
        fig.tight_layout()

        fig_name = "delta_p_x_y_theta"
        fig.savefig(os.path.join(self.address, self.seq, fig_name + '.svg'), format='svg', bbox_inches='tight', pad_inches=0.02)

    def plot_error_delta(self, p, p_gt, segment_size):
        # Assume p and p_gt are provided as NumPy arrays or convert them from PyTorch tensors
        # Example p and p_gt (replace with actual data or convert from torch)
        num_samples = p.shape[0]
        p = p.numpy()
        p_gt = p_gt.numpy()

        # Initialize lists to store RMSE, MAE, and Relative Error for each dimension (x, y, z)
        rmse_x_list, rmse_y_list, rmse_z_list = [], [], []
        mae_x_list, mae_y_list, mae_z_list = [], [], []
        rel_error_x_list, rel_error_y_list, rel_error_z_list = [], [], []

        # Segment size
        segment_size = 100

        # Iterate over the data in chunks of segment_size (100 samples each)
        for i in range(0, num_samples, segment_size):
            # Define the end index for the current segment
            end_idx = min(i + segment_size, num_samples)

            # Get the segment for both p and p_gt
            p_segment = p[i:end_idx]
            p_gt_segment = p_gt[i:end_idx]

            # Calculate the errors for the current segment
            error = p_segment - p_gt_segment

            # RMSE for x, y, z components
            rmse_x = np.sqrt(np.mean((error[:, 0]) ** 2))
            rmse_y = np.sqrt(np.mean((error[:, 1]) ** 2))
            rmse_z = np.sqrt(np.mean((error[:, 2]) ** 2))

            rmse_x_list.append(rmse_x)
            rmse_y_list.append(rmse_y)
            rmse_z_list.append(rmse_z)

            # MAE for x, y, z components
            mae_x = np.mean(np.abs(error[:, 0]))
            mae_y = np.mean(np.abs(error[:, 1]))
            mae_z = np.mean(np.abs(error[:, 2]))

            mae_x_list.append(mae_x)
            mae_y_list.append(mae_y)
            mae_z_list.append(mae_z)

            # Relative Error for x, y, z components
            rel_error_x = np.mean(np.abs(error[:, 0]) / (np.abs(p_gt_segment[:, 0]) + 1e-8))
            rel_error_y = np.mean(np.abs(error[:, 1]) / (np.abs(p_gt_segment[:, 1]) + 1e-8))
            rel_error_z = np.mean(np.abs(error[:, 2]) / (np.abs(p_gt_segment[:, 2]) + 1e-8))

            rel_error_x_list.append(rel_error_x)
            rel_error_y_list.append(rel_error_y)
            rel_error_z_list.append(rel_error_z)

        # Plot RMSE, MAE, and Relative Error for x, y, and z in three subplots
        fig, axs = plt.subplots(3, 1, figsize=(10, 15), sharex=True)

        # RMSE for x, y, z components
        axs[0].plot(np.arange(1, len(rmse_x_list) + 1), rmse_x_list, label='RMSE (x)', color='blue', marker='o')
        axs[0].plot(np.arange(1, len(rmse_y_list) + 1), rmse_y_list, label='RMSE (y)', color='red', marker='x')
        axs[0].plot(np.arange(1, len(rmse_z_list) + 1), rmse_z_list, label='RMSE (z)', color='green', marker='s')
        axs[0].set_ylabel('RMSE')
        axs[0].grid(True)
        axs[0].legend(loc='upper right')

        # MAE for x, y, z components
        axs[1].plot(np.arange(1, len(mae_x_list) + 1), mae_x_list, label='MAE (x)', color='blue', marker='o')
        axs[1].plot(np.arange(1, len(mae_y_list) + 1), mae_y_list, label='MAE (y)', color='red', marker='x')
        axs[1].plot(np.arange(1, len(mae_z_list) + 1), mae_z_list, label='MAE (z)', color='green', marker='s')
        axs[1].set_ylabel('MAE')
        axs[1].grid(True)
        axs[1].legend(loc='upper right')

        # Relative Error for x, y, z components
        axs[2].plot(np.arange(1, len(rel_error_x_list) + 1), rel_error_x_list, label='Relative Error (x)', color='blue', marker='o')
        axs[2].plot(np.arange(1, len(rel_error_y_list) + 1), rel_error_y_list, label='Relative Error (y)', color='red', marker='x')
        axs[2].plot(np.arange(1, len(rel_error_z_list) + 1), rel_error_z_list, label='Relative Error (z)', color='green', marker='s')
        axs[2].set_ylabel('Relative Error')
        axs[2].set_xlabel('Segment Number')
        axs[2].grid(True)
        axs[2].legend(loc='upper right')

        # Adjust layout
        fig.tight_layout()

        # Save figure
        fig_name = "delta_errors_xyz"
        fig.savefig(os.path.join(self.address, self.seq, fig_name + '.svg'), format='svg', bbox_inches='tight', pad_inches=0.02)


    def plot_b_omega_3(self, t, b_omega):

        fig2, axs2 = plt.subplots(3, 1, sharex=True, figsize=(16, 9))

        axs2[0].plot(t, b_omega[:, 0])
        axs2[1].plot(t, b_omega[:, 1])
        axs2[2].plot(t, b_omega[:, 2])

        axs2[0].set(xlabel='time (s)', ylabel='$\mathbf{b}_{n}^{\mathbf{\omega}_x}$ (rad/s)', title="Bias Gyro X")
        axs2[1].set(xlabel='time (s)', ylabel='$\mathbf{b}_{n}^{\mathbf{\omega}_y}$ (rad/s)', title="Bias Gyro Y")
        axs2[2].set(xlabel='time (s)', ylabel='$\mathbf{b}_{n}^{\mathbf{\omega}_z}$ (rad/s)', title="Bias Gyro Z")
        axs2[0].grid()
        axs2[1].grid()
        axs2[2].grid()
        axs2[0].legend(['$\mathbf{b}_{\mathbf{\omega}_x}$'])
        axs2[1].legend(['$\mathbf{b}_{\mathbf{\omega}_y}$'])
        axs2[2].legend(['$\mathbf{b}_{\mathbf{\omega}_z}$'])
        fig_name = "b_omega_3"
        fig2.savefig(os.path.join(self.address, self.seq, fig_name + '.png'))
        # fig2.savefig(os.path.join(self.address, self.seq, fig_name + '.eps'), format="eps")
        # fig2.clf()
        # plt.close()

    def plot_b_acc_3(self, t, b_acc):

        fig2, axs2 = plt.subplots(3, 1, sharex=True, figsize=(16, 9))

        axs2[0].plot(t, b_acc[:, 0])
        axs2[1].plot(t, b_acc[:, 1])
        axs2[2].plot(t, b_acc[:, 2])
        axs2[0].set(xlabel='time (s)', ylabel='$\mathbf{b}_n^{\mathbf{a}_x}$ (m/s^2)', title="Bias Acc X")
        axs2[1].set(xlabel='time (s)', ylabel='$\mathbf{b}_n^{\mathbf{a}_y}$ (m/s^2)', title="Bias Acc Y")
        axs2[2].set(xlabel='time (s)', ylabel='$\mathbf{b}_n^{\mathbf{a}_z}$ (m/s^2)', title="Bias Acc Z")
        axs2[0].grid()
        axs2[1].grid()
        axs2[2].grid()
        axs2[0].legend(['$b_a^x$'])
        axs2[1].legend(['$b_a^y$'])
        axs2[2].legend(['$b_a^z$'])
        fig_name = "b_acc_3"
        fig2.savefig(os.path.join(self.address, self.seq, fig_name + '.png'))
        # fig2.savefig(os.path.join(self.address, self.seq, fig_name + '.eps'), format="eps")
        # fig2.clf()
        # plt.close()

    def plot_measurements_covs(self, t, measurements_covs):

        fig2, axs2 = plt.subplots(2, 1, sharex=True, figsize=(16, 9))
        axs2[0].plot(t, measurements_covs[:, 0])
        axs2[1].plot(t, measurements_covs[:, 1])
        axs2[0].set(xlabel='time (s)', ylabel='$\mathbf{b^a}_n$ (m/s)', title="measurements_covs_y")
        axs2[1].set(xlabel='time (s)', ylabel='$\mathbf{b^a}_n$ (m/s)', title="measurements_covs_z")
        axs2[0].grid()
        axs2[1].grid()
        axs2[0].legend(['$mescov_{\mathbf{v_y}}$'])
        axs2[1].legend(['$mescov_{\mathbf{v_z}}$'])
        fig_name = "measurements_covs"
        fig2.savefig(os.path.join(self.address, self.seq, fig_name + '.png'))
        # fig2.savefig(os.path.join(self.address, self.seq, fig_name + '.eps'), format="eps")
        # fig2.clf()
        # plt.close()

    def plot_ys_b_omega_3(self, tt, ys, us_noise, us):
        t = tt[:-1]
        ys_b_omega = ys[:-1, 6:9]
        b_omega = (us_noise[:-1, :3] - us[:-1, :3])
        # gt = (ang_gt[1:] - ang_gt[:-1]) / (t[1]-t[0]) - us[:-1, :3]
        # ys_b_omega = ys[:, 9:12]
        # b_omega = (us_noise[:, 3:6] - us[:, 3:6])
        fig2, axs2 = plt.subplots(3, 1, sharex=True, figsize=(16, 9))

        axs2[0].plot(t, b_omega[:, 0])
        axs2[0].plot(t, ys_b_omega[:, 0])
        # axs2[0].plot(t, gt[:, 0])

        axs2[1].plot(t, b_omega[:, 1])
        axs2[1].plot(t, ys_b_omega[:, 1])
        # axs2[1].plot(t, gt[:, 1])

        axs2[2].plot(t, b_omega[:, 2])
        axs2[2].plot(t, ys_b_omega[:, 2])
        # axs2[2].plot(t, gt[:, 2])
        axs2[0].set(xlabel='time (s)', ylabel='$\mathbf{b}_{\mathbf{omega}_n}$ (rad/s)', title="b_omega_x")
        axs2[1].set(xlabel='time (s)', ylabel='$\mathbf{b}_{\mathbf{omega}_n}$ (rad/s)', title="b_omega_y")
        axs2[2].set(xlabel='time (s)', ylabel='$\mathbf{b}_{\mathbf{omega}_n}$ (rad/s)', title="b_omega_z")
        axs2[0].grid()
        axs2[1].grid()
        axs2[2].grid()
        # axs2[0].legend(['$ys_bomega^x$', '$us_bomega^x$', '$gt^x$'])
        # axs2[1].legend(['$ys_bomega^y$', '$us_bomega^y$', '$gt^y$'])
        # axs2[2].legend(['$ys_bomega^z$', '$us_bomega^z$', '$gt^z$'])

        axs2[0].legend(['$us_{\mathbf{b_w}}^x$', '$ys_{\mathbf{b_w}}^x$'])
        axs2[1].legend(['$us_{\mathbf{b_w}}^y$', '$ys_{\mathbf{b_w}}^y$'])
        axs2[2].legend(['$us_{\mathbf{b_w}}^z$', '$ys_{\mathbf{b_w}}^z$'])
        fig_name = "ys_b_omega_3"
        fig2.savefig(os.path.join(self.address, self.seq, fig_name + '.png'))
        # fig2.savefig(os.path.join(self.address, self.seq, fig_name + '.eps'), format="eps")

        # fig2.clf()
        # plt.close()

    def plot_ys_b_acc_3(self, tt, ys, us_noise, us):
        # ys_b_acc = ys[:, 9:12]
        # b_acc = (us_noise[:, 3:6] - us[:, 3:6])
        t = tt[:-1]
        ys_b_omega = ys[:-1, 9:12]
        b_omega = (us_noise[:-1, 3:6] - us[:-1, 3:6])
        # gt = (v_gt[1:] - v_gt[:-1]) / (t[1] - t[0]) - us[:-1, 3:6]

        fig2, axs2 = plt.subplots(3, 1, sharex=True, figsize=(16, 9))


        axs2[0].plot(t, b_omega[:, 0])
        axs2[0].plot(t, ys_b_omega[:, 0])
        # axs2[0].plot(t, gt[:, 0])

        axs2[1].plot(t, b_omega[:, 1])
        axs2[1].plot(t, ys_b_omega[:, 1])
        # axs2[1].plot(t, gt[:, 1])

        axs2[2].plot(t, b_omega[:, 2])
        axs2[2].plot(t, ys_b_omega[:, 2])
        # axs2[2].plot(t, gt[:, 2])
        axs2[0].set(xlabel='time (s)', ylabel='$\mathbf{b}^x_n$ (m/s)', title="b_acc_x")
        axs2[1].set(xlabel='time (s)', ylabel='$\mathbf{b}^y_n$ (m/s)', title="b_acc_y")
        axs2[2].set(xlabel='time (s)', ylabel='$\mathbf{b}^z_n$ (m/s)', title="b_acc_z")
        axs2[0].grid()
        axs2[1].grid()
        axs2[2].grid()
        # axs2[0].legend(['ys_bacc^x$', 'us_bacc^x$', '$gt^x$'])
        # axs2[1].legend(['ys_bacc^y$', 'us_bacc^y$', '$gt^y$'])
        # axs2[2].legend(['ys_bacc^z$', 'us_bacc^z$', '$gt^z$'])
        axs2[0].legend(['$us_{\mathbf{b_a}}^x$', 'ys_{\mathbf{b_a}}^x$'])
        axs2[1].legend(['$us_{\mathbf{b_a}}^y$', 'ys_{\mathbf{b_a}}^y$'])
        axs2[2].legend(['$us_{\mathbf{b_a}}^z$', 'ys_{\mathbf{b_a}}^z$'])
        fig_name = "ys_b_acc_3"
        fig2.savefig(os.path.join(self.address, self.seq, fig_name + '.png'))
        # fig2.savefig(os.path.join(self.address, self.seq, fig_name + '.eps'), format="eps")
        # fig2.clf()
        # plt.close()

    def plot_usfix_us_omega_3(self, tt, us_fix, us_noise, us):

        t = tt

        fig2, axs2 = plt.subplots(3, 1, sharex=True, figsize=(16, 9))


        axs2[0].plot(t, us_noise[:, 0])
        axs2[0].plot(t, us[:, 0])
        axs2[0].plot(t, us_fix[:, 0])


        axs2[1].plot(t, us_noise[:, 1])
        axs2[1].plot(t, us[:, 1])
        axs2[1].plot(t, us_fix[:, 1])


        axs2[2].plot(t, us_noise[:, 2])
        axs2[2].plot(t, us[:, 2])
        axs2[2].plot(t, us_fix[:, 2])

        axs2[0].set(xlabel='time (s)', ylabel='$\mathbf{\omega}_n^x$ (rad/s)', title="Omega X")
        axs2[1].set(xlabel='time (s)', ylabel='$\mathbf{\omega}_n^y$ (rad/s)', title="Omega Y")
        axs2[2].set(xlabel='time (s)', ylabel='$\mathbf{\omega}_n^z$ (rad/s)', title="Omega Z")
        axs2[0].grid()
        axs2[1].grid()
        axs2[2].grid()
        axs2[0].legend(['$us_{\mathbf{noise}}^x$', '$us^x$', '$us_{\mathbf{fix}}^x$'])
        axs2[1].legend(['$us_{\mathbf{noise}}^y$', '$us^y$', '$us_{\mathbf{fix}}^y$'])
        axs2[2].legend(['$us_{\mathbf{noise}}^z$', '$us^z$', '$us_{\mathbf{fix}}^z$'])

        fig_name = "usfix_us_omega_3"
        fig2.savefig(os.path.join(self.address, self.seq, fig_name + '.png'))
        # fig2.savefig(os.path.join(self.address, self.seq, fig_name + '.eps'), format="eps")
        # fig2.clf()
        # plt.close()

    def plot_usfix_us_acc_3(self, tt, us_fix, us_noise, us):

        t = tt

        fig2, axs2 = plt.subplots(3, 1, sharex=True, figsize=(16, 9))

        axs2[0].plot(t, us_noise[:, 0])
        axs2[0].plot(t, us[:, 0])
        axs2[0].plot(t, us_fix[:, 0])

        axs2[1].plot(t, us_noise[:, 1])
        axs2[1].plot(t, us[:, 1])
        axs2[1].plot(t, us_fix[:, 1])

        axs2[2].plot(t, us_noise[:, 2])
        axs2[2].plot(t, us[:, 2])
        axs2[2].plot(t, us_fix[:, 2])


        axs2[0].set(xlabel='time (s)', ylabel='$\mathbf{a}_n^x$ (m/$\mathrm{s}^2$)', title="Acc X")
        axs2[1].set(xlabel='time (s)', ylabel='$\mathbf{a}_n^y$ (m/$\mathrm{s}^2$)', title="Acc Y")
        axs2[2].set(xlabel='time (s)', ylabel='$\mathbf{a}_n^z$ (m/$\mathrm{s}^2$)', title="Acc Z")
        axs2[0].grid()
        axs2[1].grid()
        axs2[2].grid()

        axs2[0].legend(['$us_{\mathbf{noise}}^x$', '$us^x$', '$us_{\mathbf{fix}}^x$'])
        axs2[1].legend(['$us_{\mathbf{noise}}^y$', '$us^y$', '$us_{\mathbf{fix}}^y$'])
        axs2[2].legend(['$us_{\mathbf{noise}}^z$', '$us^z$', '$us_{\mathbf{fix}}^z$'])

        fig_name = "usfix_us_acc_3"
        fig2.savefig(os.path.join(self.address, self.seq, fig_name + '.png'))
        # fig2.savefig(os.path.join(self.address, self.seq, fig_name + '.eps'), format="eps")
        # fig2.clf()
        # plt.close()

    def plot_xs_hatxs_acc_3(self, t, xs, hat_xs):
        fig1, axs1 = plt.subplots(3, 1, sharex=True, figsize=(16, 9))
        axs1[0].plot(t, xs[:, 0])
        axs1[0].plot(t, hat_xs[:, 0])
        axs1[1].plot(t, xs[:, 1])
        axs1[1].plot(t, hat_xs[:, 1])
        axs1[2].plot(t, xs[:, 2])
        axs1[2].plot(t, hat_xs[:, 2])
        axs1[0].set(xlabel='time (s)', ylabel='$\mathbf{xs_\mathbf{dv_n}}$ (m/s)', title="xs_dv_x")
        axs1[1].set(xlabel='time (s)', ylabel='$\mathbf{xs_\mathbf{dv_n}}$ (m/s)', title="xs_dv_x")
        axs1[2].set(xlabel='time (s)', ylabel='$\mathbf{xs_\mathbf{dv_n}}$ (m/s)', title="xs_dv_z")
        axs1[0].grid()
        axs1[1].grid()
        axs1[2].grid()
        axs1[0].legend(['$dv_n^x$', '$\hat{dv}_n^x$'])
        axs1[1].legend(['$dv_n^y$', '$\hat{dv}_n^y$'])
        axs1[2].legend(['$dv_n^z$', '$\hat{dv}_n^z$'])
        fig_name = "dv_3"
        fig1.savefig(os.path.join(self.address, self.seq, fig_name + '.png'))
        # fig1.savefig(os.path.join(self.address, self.seq, fig_name + '.eps'), format="eps")
        # fig1.clf()
        # plt.close()

    @property
    def end_title(self):
        return " for sequence " + self.seq.replace("_", " ")

    def savefig(self, axs, fig, name):
        if isinstance(axs, np.ndarray):
            for i in range(len(axs)):
                axs[i].grid()
                axs[i].legend()
        else:
            axs.grid()
            axs.legend()
        fig.tight_layout()
        fig.savefig(os.path.join(self.address, self.seq, name + '.png'))

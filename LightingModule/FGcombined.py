from logging import log
from sys import setdlopenflags
from pytorch_lightning import LightningModule
from pytorch_lightning.trainer.supporters import CombinedLoader
from argparse import ArgumentParser
import random

import torch
from torch import nn
from torch.utils.data.dataloader import DataLoader
from torchmetrics import AUROC
from torchmetrics.functional import f1
from torch.autograd import grad
import torch.nn.functional as F

import wandb

from data.cifar10 import SplitCifar10
from data.capsule_split import get_splits as c_split
from data.mnist import SplitMNIST
from data.zeroshot_split import get_splits as z_split
from data.cifar100 import  SplitCifar100
from utils import accuracy
import numpy as np
from matplotlib import pyplot as plt

def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        m.weight.data.normal_(0.0, 0.05)
    elif classname.find('BatchNorm') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)
        
def cal_index(num_classes, y):
        batch_size = y.size(0)
        new_index = torch.randperm(batch_size).type_as(y)
        newY = y[new_index]
        mask = (newY == y)
        while mask.any().item():
            newY[mask] = torch.randint(0, num_classes, (torch.sum(mask),)).type_as(y)
            mask = (newY == y)
        return newY
        
class NoiseEncoder(nn.Module):
    def __init__(self, 
                 channel_in:  int,
                 channel_out: int,
                 kernel_size: int,
                 stride:      int,
                 padding:     int,
                 bias:        bool=False, 
                 num_classes: int=6,
                 alpha: float=1.0):
        
        super(self.__class__, self).__init__()
        self.conv = nn.Conv2d(channel_in, channel_out, kernel_size, stride, padding, bias=bias)
        self.bn = nn.BatchNorm2d(channel_out)
        self.bn_noise = nn.BatchNorm2d(channel_out)
        self.activation = nn.LeakyReLU(0.2)
        self.num_classes = num_classes
        self.register_buffer('buffer', None)
        self.alpha = alpha
        
    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        x = self.activation(x)
        return x
    
    def forward_clean(self, x, y, class_mask):
        newY = cal_index(self.num_classes, y)
        x = self.conv(x)
        self.cal_class_per_std(x, class_mask)
        noise = torch.normal(mean=0, std=self.buffer[newY]).type_as(x)
        x_n = x + self.alpha * noise
        x = self.bn(x)
        x = self.activation(x)
        
        x_n = self.bn(x_n)
        x_n = self.activation(x_n)
        return x, x_n, newY
        
    def forward_noise(self, x, newY):
        x = self.conv(x)
        x = x + self.alpha * torch.normal(mean=0, std=self.buffer[newY]).type_as(x)
        x = self.bn_noise(x)
        x = self.activation(x)
        return x
    
    def cal_class_per_std(self, x, idxs):
        std = []
        for i in range(self.num_classes):
            x_ = x[idxs[i]].clone().detach()
            
            std.append(x_.std(0))
        self.buffer = torch.stack(std)

class classifier32(nn.Module):
    def __init__(self, num_classes=2, **kwargs):
        super(self.__class__, self).__init__()
        self.num_classes = num_classes
        self.num_classes = num_classes
        
        self.conv1 = NoiseEncoder(3,     64,    3, 1, 1, bias=False, num_classes=num_classes)
        self.conv2 = NoiseEncoder(64,    64,    3, 1, 1, bias=False, num_classes=num_classes)
        self.conv3 = NoiseEncoder(64,   128,    3, 2, 1, bias=False, num_classes=num_classes)
        
        self.conv4 = NoiseEncoder(128,  128,    3, 1, 1, bias=False, num_classes=num_classes)
        self.conv5 = NoiseEncoder(128,  128,    3, 1, 1, bias=False, num_classes=num_classes)
        self.conv6 = NoiseEncoder(128,  128,    3, 2, 1, bias=False, num_classes=num_classes)
        
        self.conv7 = NoiseEncoder(128,  128,    3, 1, 1, bias=False, num_classes=num_classes)
        self.conv8 = NoiseEncoder(128,  128,    3, 1, 1, bias=False, num_classes=num_classes)
        self.conv9 = NoiseEncoder(128,  128,    3, 2, 1, bias=False, num_classes=num_classes)
        
        self.fc = nn.Linear(128, num_classes + 1)
        self.dr1 = nn.Dropout2d(0.2)
        self.dr2 = nn.Dropout2d(0.2)
        self.dr3 = nn.Dropout2d(0.2)
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        
        self.apply(weights_init)
        
    def forward(self, x):
        l1 = self.block1(x)
        l2 = self.block2(l1)
        y = self.block3(l2)
        
        return y
        
    def block1(self, x):
        x = self.dr1(x)
        x = self.conv1(x)
        x = self.conv2(x)
        x = self.conv3(x)
        
        return x
    
    def block1_n(self, x, y):
        class_mask = y.unsqueeze(0) == torch.arange(
            self.num_classes).type_as(y).unsqueeze(1)
        x = self.dr1(x)
        x = self.conv1(x)
        x = self.conv2(x)
        clean, noise, newY = self.conv3.forward_clean(x, y, class_mask)
        return clean, noise, newY
    
    def block2(self, x):
        x = self.dr2(x)
        x = self.conv4(x)
        x = self.conv5(x)
        x = self.conv6(x)
        
        return x
    
    def block2_n(self, x, y):
        class_mask = y.unsqueeze(0) == torch.arange(
            self.num_classes).type_as(y).unsqueeze(1)
        x = self.dr2(x)
        x = self.conv4(x)
        x = self.conv5(x)
        clean, noise, newY = self.conv6.forward_clean(x, y, class_mask)
        return clean, noise, newY

    def block3(self, x):
        x = self.dr3(x)
        x = self.conv7(x)
        x = self.conv8(x)
        x = self.conv9(x)

        x = self.avgpool(x)
        x = x.view(x.shape[0], -1)

        logit = self.fc(x)
        return logit
    
    def block3_(self, x):
        x = self.dr3(x)
        x = self.conv7(x)
        x = self.conv8(x)
        x = self.conv9(x)

        x = self.avgpool(x)
        x = x.view(x.shape[0], -1)
        return x
    
class ResidualBlock(nn.Module):
    def __init__(self, inplanes, planes, kernel_size=3, stride=1, padding=1, downsample=None, groups=1):
        super(ResidualBlock, self).__init__()

        self.conv1 = nn.Sequential(
            nn.Conv2d(inplanes, planes, kernel_size, stride=stride, padding=padding),
            # nn.BatchNorm2d(planes),
            nn.LeakyReLU(0.2)
        )
        self.conv2 = nn.Sequential(
            nn.Conv2d(planes, planes, kernel_size, padding=padding),
            # nn.BatchNorm2d(planes),
            nn.LeakyReLU(0.2)
        )
        self.proj = nn.Conv2d(inplanes, planes, 1) if stride==2 else None
    
    def forward(self, x):
        identity = x
        
        y = self.conv1(x)
        y = self.conv2(y)
        
        identity = identity if self.proj is None else self.proj(identity)
        y = y + identity
        return y
    
class Generator(nn.Module):
    """
        Convolutional Generator
    """
    def __init__(self, out_channel=1, n_filters=128, n_noise=512):
        super(Generator, self).__init__()
        # self.fc = nn.Linear(n_noise, 1024*4*4)
        self.G = nn.Sequential(
            # ResidualBlock(128, 128, 3, 1, 1),
            # ResidualBlock(128, 128, 3, 1, 1),
            # ResidualBlock(128, 128, 3, 1, 1),
            nn.Conv2d(128, 128, 3, 1, 1, bias=False),
            nn.BatchNorm2d(128),
            nn.LeakyReLU(0.2),
            nn.Conv2d(128, 128, 3, 1, 1, bias=False),
            nn.BatchNorm2d(128),
            nn.LeakyReLU(0.2),
            nn.Conv2d(128, 128, 3, 1, 1, bias=False),
            nn.BatchNorm2d(128),
            nn.LeakyReLU(0.2),
            # nn.BatchNorm2d(128),
        )
        
    def forward(self, x):
        out = self.G(x)
        return out + x

class Discriminator(nn.Module):
    """
        Convolutional Discriminator
    """
    def __init__(self, in_channel=1):
        super(Discriminator, self).__init__()
        self.D = nn.Sequential(
            ResidualBlock(128, 128, 3, 1, 1),
            ResidualBlock(128, 128, 3, 1, 1),
            nn.Conv2d(128, 128, 3, 2, 1),
            # nn.BatchNorm2d(128),
            nn.LeakyReLU(0.2),
            nn.AdaptiveAvgPool2d((1,1))
        )
        self.fc = nn.Linear(128, 1) # (N, 1)
        
    def forward(self, x):
        B = x.size(0)
        h = self.D(x)
        h = h.view(B, -1)
        y = self.fc(h)
        return y
    
class CenterLoss(nn.Module):
    """Center loss.
    
    Reference:
    Wen et al. A Discriminative Feature Learning Approach for Deep Face Recognition. ECCV 2016.
    
    Args:
        num_classes (int): number of classes.
        feat_dim (int): feature dimension.
    """
    def __init__(self, num_classes=10, feat_dim=2, use_gpu=True):
        super(CenterLoss, self).__init__()
        self.num_classes = num_classes
        self.feat_dim = feat_dim
        self.use_gpu = use_gpu

        if self.use_gpu:
            self.centers = nn.Parameter(torch.randn(self.num_classes, self.feat_dim).cuda())
        else:
            self.centers = nn.Parameter(torch.randn(self.num_classes, self.feat_dim))

    def forward(self, x, labels):
        """
        Args:
            x: feature matrix with shape (batch_size, feat_dim).
            labels: ground truth labels with shape (batch_size).
        """
        batch_size = x.size(0)
        distmat = torch.pow(x, 2).sum(dim=1, keepdim=True).expand(batch_size, self.num_classes) + \
                  torch.pow(self.centers, 2).sum(dim=1, keepdim=True).expand(self.num_classes, batch_size).t()
        distmat.addmm_(x, self.centers.t(), beta=1, alpha=-2)


        classes = torch.arange(self.num_classes).long()
        if self.use_gpu: classes = classes.cuda()
        labels = labels.unsqueeze(1).expand(batch_size, self.num_classes)
        mask = labels.eq(classes.expand(batch_size, self.num_classes))

        dist = distmat * mask.float()
        loss = dist.clamp(min=1e-12, max=1e+12).sum() / batch_size

        return loss

class FGcombined(LightningModule):
    def __init__(self,
                 lr: float = 0.01,
                 momentum: float = 0.9,
                 weight_decay: float = 1e-4,
                 data_dir: str = '/datasets',
                 dataset: str = 'cifar10',
                 batch_size: int = 128,
                 num_workers: int = 48,
                 class_split: int = 0,
                 latent_size: int = 128,
                 alpha: float = 0.5,
                 beta: float = 0.5,
                 gan_lr: float = 0.0002,
                 log_dir: str = './log',
                 r1_gamma: float = 10.,
                 splits: str = 'zero',
                 Mtemp: float = 0.3,
                 **kwargs):
        
        super(self.__class__, self).__init__()
        
        self.save_hyperparameters()
        self.lr = lr
        self.momentum = momentum
        self.weight_decay = weight_decay
        self.data_dir = data_dir
        self.dataset = dataset
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.class_split = class_split
        self.latent_size = latent_size
        self.alpha = alpha
        self.beta = beta
        self.gan_lr = gan_lr
        self.r1_gamma = r1_gamma
        self.log_dir = log_dir
        self.split_case = splits
        self.Mtemp = Mtemp
        
        if self.split_case == 'zero':
            self.splits = z_split(dataset, class_split)
        elif self.split_case == 'cvae':
            self.splits = c_split(dataset, class_split)
        self.num_classes = len(self.splits['known_classes'])
        
        self.model = classifier32(num_classes=len(self.splits['known_classes']))
        self.NewFC = nn.Sequential(
            nn.Linear(self.latent_size, self.latent_size * 2),
            nn.Linear(self.latent_size * 2, self.latent_size),
            nn.Linear(self.latent_size, 1),
            nn.Sigmoid()
        )
        self.Centers = CenterLoss(self.num_classes, self.latent_size, True)
        self.G = Generator()
        self.G2 = Generator()
        # self.D = Discriminator()
        self.criterionD = nn.BCELoss()

        self.criterion = nn.CrossEntropyLoss()
        self.kld = nn.KLDivLoss(reduction='batchmean')
        self.auroc1 = AUROC(pos_label=1)
        self.auroc2 = AUROC(pos_label=1)
        self.auroc3 = AUROC(pos_label=1)
        
        self.automatic_optimization = False

    def on_train_start(self) -> None:
        wandb.save(self.log_dir+f"/{__file__.split('/')[-1]}")
        
        # print(f"load state_dict")
        # if 'cifar+' in self.dataset:
        #     dataset = 'cifar+10'
        #     split_case = 'zero'
        # else:
        #     dataset = self.dataset
        #     split_case = self.class_split
        # weight_path = f"./result/{dataset}/{split_case}_s{self.class_split}.ckpt"
        # state_dict = torch.load(weight_path)['state_dict']
        
        # # self.load_state_dict(state_dict)
        # # self.model.load_state_dict(state_dict) 
        # model_state_dict = self.model.state_dict()
        # for name, param in state_dict.items():
        #     if 'model' in name:
        #         n = name.replace('model.', '')
        #         if 'fc' in n:
        #             continue
        #         if n in model_state_dict.keys():
        #             model_state_dict[n].copy_(param)
        
    def forward(self, x):
        out = self.model(x)
        return out

        
    def training_step(self, batch, batch_idx):
        x, y = batch
        # print(self.trainer.current_epoch)
        batch_size = x.size(0)

        opt_C, opt_G = self.optimizers()
        
        # scheduler = self.lr_schedulers()
        log_dict = dict()
        
        #############################################
        # Update Model Classifier
        #############################################
        opt_C.zero_grad()
        opt_G.zero_grad()
        
        # l1, noise, newY = self.model.block1_n(x, y)
        l1 = self.model.block1(x)
        fake = self.G(l1)
        concat = torch.cat([l1, fake.detach()], dim=0)
        l2 = self.model.block2(concat)
        emb = self.model.block3_(l2)
        
        # center_loss = self.Centers(emb, y)
        logits = self.model.fc(emb[:batch_size])
        logits2 = self.model.fc(emb[batch_size:])
        
        Mclass_loss = self.criterion(logits, y)
        
        Madv_loss = self.criterion(logits2, torch.ones_like(y) * self.num_classes)

        log_dict['train/Mclass loss'] = Mclass_loss
        log_dict['train/Madv loss'] = Madv_loss
        log_dict['train/closed acc'] = accuracy(logits, y)[0].item()
        # log_dict['tarin/open acc'] = accuracy(adversarial_out, adv_target)[0].item()
        # total_loss = Mclass_loss + Mreal_loss + Madv_loss + center_loss
        total_loss = Mclass_loss + Madv_loss
        # opt_C.zero_grad()
        self.manual_backward(total_loss)
        opt_C.step()
        # opt_G.step()
        
        ############################################
        # Update Fake 
        ############################################
        # requires_grad(self.model, False)
        opt_C.zero_grad()
        opt_G.zero_grad()
        l1 = self.model.block1(x)
        l1 = self.G(l1.detach())
        l3 = self.model.block2(l1)
        emb = self.model.block3_(l3)
        logit = self.model.fc(emb)
        # rf = self.NewFC(emb)
        
        # real = self.criterionD(rf.squeeze(), torch.ones_like(y).float())
        Gadv_loss = self.criterion(logit, y)
        
        Ftotal_loss = Gadv_loss
        
        self.manual_backward(Ftotal_loss)
        opt_G.step()
        
        self.log_dict(log_dict)
        # if self.trainer.is_last_batch:

        #     wandb.log({
        #         'img': [wandb.Image(real[0][0].detach().cpu().numpy(), caption='clean'),
        #                 wandb.Image(noise[0][0].detach().cpu().numpy(), caption='noise'),
        #                 wandb.Image(fake[0][0].detach().cpu().numpy(), caption='fake'),]
        #     })
            
        # if self.trainer.is_last_batch:
        #     scheduler.step()
        
        return None
        
       
    def validation_step(self, batch, batch_idx, dataloader_idx):
        out_dict = {}
        log_dict = {}
        if dataloader_idx == 0:
            x, y = batch
            out = self.model(x)
            out_dict['closed_out'] = out
            out_dict['closed_y'] = y
            loss = self.criterion(out, y)
            log_dict['val/loss'] = loss
        elif dataloader_idx == 1:
            x, y, = batch
            out = self.model(x)
            out_dict['open_out'] = out
            out_dict['open_y'] = torch.ones_like(y) * self.num_classes
        
        self.log_dict(log_dict)
        
        return out_dict

    def validation_epoch_end(self, outputs) -> None:
        # closed_out = outputs[0]
        # open_out = outputs[1]
    
        c_out = torch.cat([logits['closed_out'] for logits in outputs[0]], dim=0)
        c_target = torch.cat([target['closed_y'] for target in outputs[0]], dim=0)
        o_out = torch.cat([logits['open_out'] for logits in outputs[1]], dim=0)
        o_target = torch.cat([target['open_y'] for target in outputs[1]], dim=0)
        
        total_out = torch.cat([c_out, o_out], dim=0)
        total_target = torch.cat([c_target, o_target], dim=0)
        known_target = torch.ones_like(c_target)
        unknown_target = torch.zeros_like(o_target)
        auroc_target = torch.cat([known_target, unknown_target], dim=0)
        
        softmax = self.auroc1(total_out.softmax(-1)[:,:-1].max(-1)[0], auroc_target)
        open_logit = self.auroc2(total_out.softmax(-1)[:,-1], auroc_target)
        logit = self.auroc3(total_out[:,:-1].max(-1)[0], auroc_target)
        
        f1_result = f1(total_out.max(-1)[1], total_target, average='macro', num_classes=self.num_classes+1)
        known_acc = accuracy(c_out, c_target)[0]
        unknown_acc = accuracy(o_out, o_target)[0]
        
        f1_table = []
        soft_table = []
        logit_table = []
        closed_table = []
        open_table = []
        biases = np.arange(0, 1, 0.01)
        for bias in biases:
            cal_out = calibrate_only(total_out, bias)
            f1_score_data = (f1(cal_out.max(-1)[1], total_target, average='macro', num_classes=self.num_classes+1) * 100).detach().cpu().numpy()
            softmax_data = (self.auroc1(cal_out.softmax(-1)[:,:-1].max(-1)[0], auroc_target) * 100).detach().cpu().numpy()
            logit_data = (self.auroc2(cal_out[:,:-1].max(-1)[0], auroc_target) * 100).detach().cpu().numpy()
            closed_acc = accuracy(cal_out[:len(c_target)], c_target)[0].detach().cpu().numpy()
            open_acc = accuracy(cal_out[len(c_target):], o_target)[0].detach().cpu().numpy()
            f1_table.append(f1_score_data)
            soft_table.append(softmax_data)
            logit_table.append(logit_data)
            closed_table.append(closed_acc)
            open_table.append(open_acc)
            del cal_out
        
        plt.plot(biases, f1_table, label='f1')
        plt.plot(biases, soft_table, label='softmax')
        plt.plot(biases, logit_table, label='logit')
        plt.plot(biases, closed_table, label='closed')
        plt.plot(biases, open_table, label='open')
        plt.legend()
        wandb.log({
            'calibration': wandb.Image(plt, caption='bias calibrate')})
        plt.clf()
        plt.cla()
        plt.close()
        

        log_dict = {
            'val/f1': f1_result,
            'val/known acc': known_acc,
            'val/unknown acc': unknown_acc,
            'val/softmax': softmax,
            'val/open_logit': open_logit,
            'val/logit': logit
        }
        self.log_dict(log_dict)
        
        return
    
    
    
    def configure_optimizers(self):
        # params = self.parameters()
        
        # optimizer = torch.optim.SGD(params, lr=self.lr, momentum=self.momentum, 
        #                              weight_decay=self.weight_decay)
        
        C_params = list(self.model.parameters()) + list(self.NewFC.parameters()) + list(self.Centers.parameters())
        optimizer = torch.optim.Adam(C_params,
                                 lr=self.lr, weight_decay=self.weight_decay)
        
        opt_G = torch.optim.Adam(self.G.parameters(), lr=self.lr, weight_decay=self.weight_decay)
        
        # opt_G = torch.optim.Adam(self.G.parameters(), lr=0.001,
        #                          betas=(0.5, 0.999))
        # opt_D = torch.optim.Adam(self.D.parameters(), lr=0.0001,
        #                          betas=(0.5, 0.999))
        # opt_G = torch.optim.RMSprop(self.G.parameters(), lr=1e-4, alpha=0.99)
        # opt_D = torch.optim.RMSprop(self.D.parameters(), lr=1e-4, alpha=0.99)
        
        
        # # REDUCE LR ON PLATEAU
        # scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        #     optimizer, 
        #     mode='min', 
        #     factor=0.5, 
        #     patience=10
        #     )
        
        # # COSINE ANNEALING
        # scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=100, eta_min=1e-6)

        # MULTI SETP LR
        scheduler = torch.optim.lr_scheduler.MultiStepLR(
            optimizer,
            milestones=[30, 60, 90],
            gamma=0.1
        )
        
        # return {
        #     'optimizer': [optimizer, opt_G, opt_D]
        #     # 'lr_scheduler': {
        #     #     'scheduler': scheduler,
        #     #     # 'monitor': 'val_loss'
        #     # }
        # }
        return [optimizer, opt_G], [scheduler]
        
    def train_dataloader(self):
        if self.dataset == 'cifar10' or self.dataset == 'cifar+10' or self.dataset == 'cifar+50':
            from data.cifar10 import train_transform
            dataset = SplitCifar10(self.data_dir, train=True,
                                   transform=train_transform, download=True)
            dataset.set_split(self.splits['known_classes'])
            
        elif self.dataset == 'mnist':
            from data.mnist import train_transform
            dataset = SplitMNIST(self.data_dir, train=True,
                                 transform=train_transform, download=True, split=self.splits['known_classes'])  
        else:
            raise ValueError(f"{self.dataset} is not possible")
        return DataLoader(dataset, batch_size=self.batch_size, num_workers=self.num_workers)
    
    
    
    def val_dataloader(self):
        if self.dataset == 'cifar10':
            from data.cifar10 import val_transform
            dataset_closed = SplitCifar10(self.data_dir, train=False,
                                      transform=val_transform, split=self.splits['known_classes'],
                                      download=True)
            dataset_open = SplitCifar10(self.data_dir, train=False,
                                      transform=val_transform, split=self.splits['unknown_classes'], 
                                      download=True)
            closed = DataLoader(dataset_closed, batch_size=self.batch_size, num_workers=self.num_workers)
            opened = DataLoader(dataset_open, batch_size=self.batch_size, num_workers=self.num_workers)
            return [closed, opened]
            
        elif self.dataset == 'cifar+10' or self.dataset == 'cifar+50':
            from data.cifar10 import val_transform

            dataset_closed = SplitCifar10(self.data_dir, train=False,
                                       transform=val_transform, split=self.splits['known_classes'],
                                       download=True)
            dataset_open =SplitCifar100(self.data_dir, train=False,
                                        transform=val_transform, split=self.splits['unknown_classes'],
                                        download=True)
            closed = DataLoader(dataset_closed, batch_size=self.batch_size, num_workers=self.num_workers)
            opened = DataLoader(dataset_open, batch_size=self.batch_size, num_workers=self.num_workers)
            
            return [closed, opened]
        
        elif self.dataset == 'mnist':
            from data.mnist import val_transform
            dataset_c = SplitMNIST(self.data_dir, train=False,
                                   transform=val_transform, split=self.splits['known_classes'])
            dataset_o = SplitMNIST(self.data_dir, 
                                   transform=val_transform, split=self.splits['unknown_classes'])
            
            closed = DataLoader(dataset_c, batch_size=self.batch_size, num_workers=self.num_workers)
            opened = DataLoader(dataset_o, batch_size=self.batch_size, num_workers=self.num_workers)
            return [closed, opened]
        
        else:
            raise ValueError(f'{self.dataset} is not defined')

        
    def kldiv(self, P, Q):
        return (P * (P / Q).log()).sum(-1)
    
    def r1loss(self, inputs, label=None):
        # non-saturating loss with R1 regularization
        l = -1 if label else 1
        return F.softplus(l*inputs).mean()
       
    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = ArgumentParser(parents=[parent_parser], add_help=False)
        
        parser.add_argument("--lr", type=float, default=0.0001)
        parser.add_argument("--latent_dim", type=int, default=128)
        parser.add_argument("--momentum", type=float, default=0.9)
        parser.add_argument("--weight_decay", type=float, default=1e-4)

        parser.add_argument("--batch_size", type=int, default=256)
        parser.add_argument("--num_workers", type=int, default=20)
        parser.add_argument("--data_dir", type=str, default="/datasets")
        parser.add_argument("--class_split", type=int, default=0)
        
        parser.add_argument("--alpha", type=float, default=0.5)
        parser.add_argument("--beta", type=float, default=1.0)
        parser.add_argument("--gan_lr", type=float, default=0.001)
        
        parser.add_argument("--Mtemp", type=float, default=0.3)

        return parser
    
def requires_grad(model, flag=True):
    for p in model.parameters():
        p.requires_grad = flag
        
def calibrate_only(o, bias, index=-1):
    a = o.clone()
    a[:,index] -= bias
    return a
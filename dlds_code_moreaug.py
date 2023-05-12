# -*- coding: utf-8 -*-
"""DLDS resnet.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1y6pIBJIF_3zrFzY0LdaTwOilopWesdxm
"""

import os
import time
import pandas as pd
from torchvision.io import read_image
import torch
from torch.utils.data import Dataset
from torch.utils.data import DataLoader
from torchvision import datasets
from torchvision.transforms import ToTensor
import matplotlib.pyplot as plt
from torch import nn
from torch.utils.tensorboard import SummaryWriter
from tqdm.notebook import tqdm
import torchvision
import torchvision.transforms as transforms
import datetime
import sys
import yaml
import optuna
import numpy as np
import pickle as pkl

def np_to_tensor(x, device):
    # allocates tensors from np.arrays
    if device == 'cpu':
        return torch.from_numpy(x).cpu()
    else:
        return torch.from_numpy(x).contiguous().pin_memory().to(device=device, non_blocking=True)

class FaceDataset(Dataset):
    def __init__(self, annotations_file, img_dir, transform=None, target_transform=None, output_category="gender", balanced=False):
        self.img_labels = pd.read_csv(annotations_file)
        if balanced:
            df: pd.DataFrame = self.img_labels
            self.img_labels = df[df['service_test']]
        self.img_dir = img_dir
        self.transform = transform
        self.target_transform = target_transform
        self.output_category = output_category

    def __len__(self):
        return len(self.img_labels)

    def __getitem__(self, idx):
        #print(idx)
        img_path = os.path.join(self.img_dir, self.img_labels.iloc[idx, 0])
        image = (read_image(img_path)/255).to(device=device, non_blocking=True)
        #one-hot-encoding
        #label=torch.tensor(int(self.img_labels.iloc[idx, 2]=='Female'))
        #label=torch.nn.functional.one_hot(label,num_classes=2)
        if self.output_category == "gender" or self.output_category == "combined":
            label = torch.tensor(int(self.img_labels.iloc[idx, 2] == 'Female'))
            gender_label = torch.nn.functional.one_hot(label, num_classes=2)
        if self.output_category == "race" or self.output_category == "combined":
            ethnicity = self.img_labels.iloc[idx, 3]
            label = 0
            if ethnicity == 'Black':
                label = 0
            elif ethnicity == "East Asian":
                label = 1
            elif ethnicity == "Indian":
                label = 2
            elif ethnicity == "Latino_Hispanic":
                label = 3
            elif ethnicity == "Middle Eastern":
                label = 4
            elif ethnicity == "Southeast Asian":
                label = 5
            elif ethnicity == "White":
                label = 6
            else:
                print("Problem: ethnicity label not known for index " + str(idx))
            label = torch.tensor(label)
            ethnicity_label = torch.nn.functional.one_hot(label, num_classes=7)
        else:
            print("no valid output_category")
        if(self.output_category == "gender"):
            label=gender_label.float().to(device=device, non_blocking=True)
        if(self.output_category == "race"):
            label = ethnicity_label.float().to(device=device, non_blocking=True)
        if (self.output_category == "combined"):
            label = (ethnicity_label.float().to(device=device, non_blocking=True), gender_label.float().to(device=device, non_blocking=True))
        #label=label.float().to(device=device, non_blocking=True)
        if self.transform:
            image = self.transform(image)
        if self.target_transform:
            label = self.target_transform(label)
        return image, label

def freeze_bn_module_params(module):
    if isinstance(module, torch.nn.modules.batchnorm._BatchNorm):
        #print("freeze_bn_params")
        #print(module)
        for param in module.parameters():
            #print(param.requires_grad)
            param.requires_grad = False
            #print(param.requires_grad)

def set_bn_estimate_to_eval(module):
    if isinstance(module, torch.nn.modules.batchnorm._BatchNorm):
        #print("bn_eval")
        #print(module.training)
        module.eval()
        #print(module.training)

# def load_model(num_classes, layers_to_train=[], train_bn_params=True, update_bn_estimate=True):
#     #load resnet. depth 18, 34, 50, 101, 152
#     model = torchvision.models.resnet18(pretrained=True)
#     #model = torch.hub.load('pytorch/vision:v0.10.0', 'resnet18', pretrained=True)
#     #model = torch.hub.load('pytorch/vision:v0.10.0', 'resnet18', weights=torchvision.models.ResNet18_Weights.IMAGENET1K_V1)
#     #adapt the last layer to number of classes
#     model.fc = torch.nn.Sequential(torch.nn.Linear(in_features=512, out_features=num_classes, bias=True), torch.nn.Softmax(dim=1))
#     #specify which layers to train
#     if layers_to_train!=[]:
#         for param in model.parameters():
#             param.requires_grad = False
#         for l in layers_to_train:
#             #print(getattr(model, l))
#             for param in getattr(model, l).parameters():
#                 param.requires_grad = True
#     if not train_bn_params:
#         model.apply(freeze_bn_module_params)
#     if not update_bn_estimate:
#         model.apply(set_bn_estimate_to_eval)
#     return model.to(device)

class FaceResNet18(nn.Module):
  def __init__(self):
        super().__init__()
        #load pretrained model
        self.net = torchvision.models.resnet18(pretrained=True)
        #torch.hub.load('pytorch/vision:v0.10.0', 'resnet18', weights=torchvision.models.ResNet18_Weights.IMAGENET1K_V1)
        #build the two prediction heads for multi task learning
        self.net.fc = nn.Identity()
        self.net.fc_race = torch.nn.Sequential(torch.nn.Linear(in_features=512, out_features=7, bias=True), torch.nn.Softmax(dim=1))
        self.net.fc_gender = torch.nn.Sequential(torch.nn.Linear(in_features=512, out_features=2, bias=True), torch.nn.Softmax(dim=1))
        print(self)

  def forward(self, x):
        gender_head = self.net.fc_gender(self.net(x))
        ethnicity_head = self.net.fc_race(self.net(x))
        return (ethnicity_head, gender_head)

def train(train_dataloader, eval_dataloader, model, loss_fn, metric_fns, optimizer, n_epochs, trial=None):
    #if do_tuning:
    global lowest_val_loss
    # training loop
    logdir = './tensorboard/net'
    writer = SummaryWriter(logdir)  # tensorboard writer (can also log images)

    history = {}  # collects metrics at the end of each epoch

    for epoch in range(n_epochs):  # loop over the dataset multiple times
        print('Starting epoch ' + str(epoch))

        # initialize metric list
        metrics = {'loss': [], 'val_loss': []}
        for k, _ in metric_fns.items():
            metrics[k] = []
            metrics['val_'+k] = []

        # training
        model.train()
        for (x, y) in train_dataloader:#was pbar
            optimizer.zero_grad()  # zero out gradients
            if(output_category=='combined'):
                x_aug = x.clone()
                y_race_aug, y_gender_aug = y[0].clone(),y[1].clone()
                y_aug=y_race_aug, y_gender_aug
            else:
                x_aug=x.clone()
                y_aug=y.clone()
            if(use_data_augmentation):
                x_aug=data_augmentation(x_aug,p_augment)
            if(use_mix_up or use_cut_mix):
                indices = torch.randperm(x.size(0))
                shuffled_x = x[indices]
                shuffled_y = y[indices]
                alpha = 0.2
                dist = torch.distributions.beta.Beta(alpha, alpha)
                if (np.random.normal() < p_augment and use_cut_mix):
                    x_aug,y_aug = cutMix(x_aug, y_aug, shuffled_x, shuffled_y, dist)
                if (np.random.normal() < p_augment and use_mix_up):
                    x_aug, y_aug = mixUp(x_aug, y_aug, shuffled_x, shuffled_y, dist)

            y_hat = model(x_aug)  # forward pass
            loss = loss_fn(y_hat, y_aug)
            loss.backward()  # backward pass
            optimizer.step()  # optimize weights
            #print("step")
            # log partial metrics
            metrics['loss'].append(loss.item())
            for k, fn in metric_fns.items():
                metrics[k].append(fn(y_hat, y).item())

        # validation
        #if do_tuning:
        loss_sum = 0 #for pruning
        model.eval()
        with torch.no_grad():  # do not keep track of gradients
            for (x, y) in eval_dataloader:
                y_hat = model(x)  # forward pass
                loss = loss_fn(y_hat, y)
                # log partial metrics
                metrics['val_loss'].append(loss.item())
                for k, fn in metric_fns.items():
                    metrics['val_'+k].append(fn(y_hat, y).item())
                #if do_tuning:
                    loss_sum += metrics['val_loss'][-1]/len(eval_dataloader)
            if do_tuning:
                # log loss for pruning
                trial.report(loss_sum, epoch)
                if trial.should_prune():
                    raise optuna.exceptions.TrialPruned()

        # summarize metrics, log to tensorboard and display
        history[epoch] = {k: sum(v) / len(v) for k, v in metrics.items()}
        for k, v in history[epoch].items():
          writer.add_scalar(k, v, epoch)
        print(' '.join(['\t- '+str(k)+' = '+str(v)+'\n ' for (k, v) in history[epoch].items()]))

    print('Finished Training')
    val_loss = history[n_epochs-1]['val_loss']
    if (not do_tuning) or val_loss < lowest_val_loss:
        lowest_val_loss = val_loss
        # plot loss curves
        fig, ax = plt.subplots(1)
        ax.plot([v['loss'] for k, v in history.items()], label='Training Loss')
        ax.plot([v['val_loss'] for k, v in history.items()], label='Validation Loss')
        ax.set_ylabel('Loss')
        ax.set_xlabel('Epochs')
        ax.set_title("Loss for config file= " + str(configfilename))
        ax.legend()
        # fig.show()
        # fig.savefig('train_val_graph_inclfc_test.png')
        graphname = "Loss_graph_" + str(configfilename) + "_" + ct + ".png"
        print("Saved loss graph with filename: " + graphname)
        fig.savefig(graphname)
        #plot other metrics
        for metricname, _ in metric_fns.items():
            fig2, ax2 = plt.subplots(1)
            ax2.plot([v[metricname] for k, v in history.items()], label=('Training ' + metricname))
            ax2.plot([v['val_' + metricname] for k, v in history.items()], label=('Validation ' + metricname))
            ax2.set_ylabel(metricname)
            ax2.set_xlabel('Epochs')
            ax2.set_title(str(metricname + " for config file= " + str(configfilename)))
            ax2.legend()
            # fig2.show()
            graphname = metricname + "_graph_" + str(configfilename) + "_" + ct + ".png"
            print("Saved " + metricname + " graph with filename: " + graphname)
            fig2.savefig(graphname)

        test_pred = []
        test_truth = []
        model.eval()
        with torch.no_grad():  # do not keep track of gradients
            for (x, y) in test_dataloader:
                test_pred.append(model(x))  # forward pass
                test_truth.append(y)
        # save predictions TODO file name
        filename = str(configfilename) + "_" + ct
        pred_file = open("predictions_" + filename + ".pkl", "wb")  # create new file if this doesn't exist yet
        truth_file = open("groundtruth_" + filename + ".pkl", "wb")  # create new file if this doesn't exist yet
        pkl.dump(test_pred, pred_file)
        pkl.dump(test_truth, truth_file)
        pred_file.close()
        truth_file.close()

    return lowest_val_loss

def accuracy_fn(y_hat, y):
    # computes classification accuracy
    return (torch.argmax(y_hat, dim=1) == torch.argmax(y, dim=1)).float().mean()

# more data augmentation options at https://pytorch.org/vision/stable/transforms.html
def data_augmentation(image, prob):
  translayers = transforms.RandomApply(
      torch.nn.Sequential(
        torchvision.transforms.RandomHorizontalFlip(0.5),
        torchvision.transforms.ColorJitter(0.2, 0.15, 0.15, 0.05),
        #torchvision.transforms.RandomRotation(8),
        transforms.RandomAffine(6, translate=(0.1,0.1), shear=5),
        transforms.RandomApply(
            torch.nn.Sequential(
            transforms.RandomCrop(200),
            transforms.Resize(256)
            ),p=0.4
        )
        ), p=prob
  )

  return translayers(image)

# inspired by https://towardsdatascience.com/cutout-mixup-and-cutmix-implementing-modern-image-augmentations-in-pytorch-a9d7db3074ad
def cutMix(data_orig, labels, shuffled_data, shuffled_labels, dist):
    lam = dist.sample()
    bbx1, bby1, bbx2, bby2 = rand_bbox(data_orig.size(), lam)
    mixed=data_orig.clone()
    mixed[:, :, bbx1:bbx2, bby1:bby2] = shuffled_data[:, :, bbx1:bbx2, bby1:bby2]
    # adjust lambda to exactly match pixel ratio
    lam = 1 - ((bbx2 - bbx1) * (bby2 - bby1) / (data_orig.size()[-1] * data_orig.size()[-2]))
    y_l = torch.full(labels.size(), lam).to(device=device)
    new_targets = labels * y_l + shuffled_labels * (1 - y_l)
    return mixed, new_targets

def rand_bbox(size, lam):
    W = size[2]
    H = size[3]
    cut_rat = np.sqrt(1. - lam)
    cut_w = int(W * cut_rat)
    cut_h = int(H * cut_rat)

    # uniform
    cx = np.random.randint(W)
    cy = np.random.randint(H)

    bbx1 = np.clip(cx - cut_w // 2, 0, W)
    bby1 = np.clip(cy - cut_h // 2, 0, H)
    bbx2 = np.clip(cx + cut_w // 2, 0, W)
    bby2 = np.clip(cy + cut_h // 2, 0, H)

    return bbx1, bby1, bbx2, bby2

# inspired by https://keras.io/examples/vision/mixup/ and https://towardsdatascience.com/cutout-mixup-and-cutmix-implementing-modern-image-augmentations-in-pytorch-a9d7db3074ad
def mixUp(data, labels, shuffled_data, shuffled_labels, dist):
    # Sample lambda and reshape it to do the mixup
    l = dist.sample()
    print(l)
    l=0.5
    x_l = torch.full(data.size(),l).to(device=device)
    y_l = torch.full(labels.size(),l).to(device=device)
    # Perform mixup on both images and labels by combining a pair of images/labels
    # (one from each dataset) into one image/label
    images = data * x_l + shuffled_data * (1 - x_l)
    labels = labels * y_l + shuffled_labels * (1 - y_l)
    return (images, labels)

def bce_loss(yhat, y):
    if(type(yhat) is tuple):
        race_label,gender_label=y
        race_label_hat,gender_label_hat=yhat
        l1=nn.BCELoss(race_label_hat, race_label)
        l2=nn.BCELoss(gender_label_hat, gender_label)
        return (l1+l2)/2
    else:
        return nn.BCELoss(yhat, y)

 # Define a set of hyperparameter values, build the model, train the model, and evaluate the accuracy
def objective(trial):
    params = {
        'start_learningrate': trial.suggest_loguniform('start_learningrate', 0.0001, 0.002),
        'n_epochs': trial.suggest_int('n_epochs',5,15),
        'batch_size': trial.suggest_categorical('batch_size',[64, 128]),
        'layer_to_train_option': trial.suggest_categorical("layer_to_train_option", ["all", "layer3"]),
        'train_bn_params': trial.suggest_categorical("train_bn_params", [False, True]),
        'update_bn_estimate': trial.suggest_categorical("update_bn_estimate", [False, True])
    }
    if params['layer_to_train_option'] =="all":
        layers_to_train = []
    elif params['layer_to_train_option'] =="layer3":
        layers_to_train = ["layer3", "layer4", "fc"]
    elif params['layer_to_train_option'] == "fc":
        layers_to_train = ["fc"]
    else:
        print("No valid layer_to_train_option")
    print("Params in current trial:")
    print(params)
    train_dataloader = DataLoader(training_data, batch_size=params['batch_size'], shuffle=True)
    print("Train datasets loaded")
    #model=load_model(num_classes, layers_to_train, params["train_bn_params"], params["update_bn_estimate"])
    model = FaceResNet18().to(device=device)
    print("Model loaded")
    loss_fn = bce_loss
    metric_fns = {'acc': accuracy_fn}
    optimizer = torch.optim.Adam(model.parameters(), lr=params["start_learningrate"])
    start = time.time()
    score = train(train_dataloader, val_dataloader, model, loss_fn, metric_fns, optimizer, params["n_epochs"], trial)
    end = time.time()
    print("Time in minutes for training "+str(params["n_epochs"])+" epochs:")
    print((end - start)/60)
    return score

if __name__ == "__main__":
    device = (
        "cuda"
        if torch.cuda.is_available()
        else "mps"
        if torch.backends.mps.is_available()
        else "cpu"
    )
    print("Running on " + device)

    ct = str(datetime.datetime.now())
    ct = ct.replace(" ", "_")
    ct = ct.replace(".", "_")
    ct = ct.replace(":", "-")

    if len(sys.argv)>1:
        configfilename = sys.argv[1]
        file = open("configs/"+configfilename + ".yaml", 'r')
        config_dict = yaml.safe_load(file)
        print("Using config file named " + configfilename + " with configurations: ")
        print(config_dict)
    else:
        configfilename = "config_default"
        config_dict = {}
        print("No config file provided, using default")

    layers_to_train = config_dict.get("layers_to_train", [])
    output_category = config_dict.get("output_category", "gender")
    use_data_augmentation=config_dict.get("use_data_augmentation", False)
    use_balanced_dataset = config_dict.get("use_balanced_dataset", False)
    batch_size=config_dict.get("batch_size", 64)
    start_learningrate = config_dict.get("start_learningrate", 0.001)
    n_epochs = config_dict.get("n_epochs", 15)
    data_path=config_dict.get("data_path", 'DD2424_data')
    use_short_data_version = config_dict.get("use_short_data_version", False)
    train_bn_params = config_dict.get("train_bn_params", True)
    update_bn_estimate = config_dict.get("update_bn_estimate", True)
    use_cut_mix = config_dict.get("use_cut_mix", False)
    use_mix_up = config_dict.get("use_mix_up", False)
    p_augment = config_dict.get("p_augment", 0.5)
    n_optuna_trials = config_dict.get("n_optuna_trials", 1)
    do_tuning = config_dict.get("do_tuning", False)

    if output_category == 'gender':
        num_classes = 2
    elif output_category == "race":
        num_classes=7
    elif output_category == "combined":
        num_classes = [7,2]
    else:
        print("Invalid output_category")

    if use_short_data_version:
        labelfileprev = "short_version_"
    else:
        labelfileprev = ""

    if use_short_data_version:
        training_data = FaceDataset(data_path + "/" + labelfileprev + "fairface_label_train.csv", data_path,
                                    output_category=output_category, balanced=use_balanced_dataset)
        val_data = FaceDataset(data_path + "/" + labelfileprev + "fairface_label_val.csv", data_path, output_category=output_category,
                               balanced=use_balanced_dataset)
        test_data = FaceDataset(data_path + "/test.csv", data_path,
                                output_category=output_category, balanced=use_balanced_dataset)
    else:
        training_data = FaceDataset(data_path+"/train.csv", data_path, output_category=output_category, balanced=use_balanced_dataset)
        val_data = FaceDataset(data_path+"/val.csv", data_path, output_category=output_category, balanced=use_balanced_dataset)
        test_data = FaceDataset(data_path + "/test.csv", data_path,output_category=output_category, balanced=use_balanced_dataset)
    val_dataloader = DataLoader(val_data, batch_size=128, shuffle=False)
    test_dataloader = DataLoader(test_data, batch_size=128, shuffle=False)
    lowest_val_loss = 1
    if do_tuning:
        study = optuna.create_study(direction="minimize", sampler=optuna.samplers.TPESampler(),
                                    pruner=optuna.pruners.MedianPruner())
        study.optimize(objective, n_trials=n_optuna_trials)  # -> function given by objective
        best_trial = study.best_trial
        for key, value in best_trial.params.items():
            print("{}: {}".format(key, value))
    else:
        train_dataloader = DataLoader(training_data, batch_size=batch_size, shuffle=True)
        print("Datasets loaded")
        #model = load_model(num_classes, layers_to_train, train_bn_params, update_bn_estimate)
        model = FaceResNet18().to(device=device)
        print("Model loaded")
        loss_fn = bce_loss
        metric_fns = {'acc': accuracy_fn}
        optimizer = torch.optim.Adam(model.parameters(), lr=start_learningrate)
        start = time.time()
        score = train(train_dataloader, val_dataloader, model, loss_fn, metric_fns, optimizer, n_epochs)
        end = time.time()
        print("Time in minutes for training " + str(n_epochs) + " epochs:")
        print((end - start) / 60)
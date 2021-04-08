"""

    Adapted from:
        https://github.com/dl4sits/BreizhCrops

    Original implementation(s) of TempCNN model:
        https://github.com/dl4sits/BreizhCrops/blob/master/breizhcrops/models/LongShortTermMemory.py
        https://github.com/charlotte-pel/temporalCNN

"""

import os

import torch
import torch.nn as nn
import torch.utils.data

from ..base import BaseMulticlassClassifier
from .schemas import TempCNNSchema

#__all__ = ['TempCNN']

class TempCNN(BaseMulticlassClassifier):

    schema = TempCNNSchema

    def __init__(self, config):

        BaseMulticlassClassifier.__init__(self, config)

        #self.modelname = f"TempCNN_input-dim={input_dim}_num-classes={num_classes}_sequencelenght={sequencelength}_" \
        #                 f"kernelsize={kernel_size}_hidden-dims={hidden_dims}_dropout={dropout}"

        self.model.conv_bn_relu1 = Conv1D_BatchNorm_Relu_Dropout(self.config.input_dim, self.config.input_dim, kernel_size=self.config.kernel_size,
                                                           drop_probability=self.config.dropout)
        self.model.conv_bn_relu2 = Conv1D_BatchNorm_Relu_Dropout(self.config.input_dim, self.config.input_dim, kernel_size=self.config.kernel_size,
                                                           drop_probability=self.config.dropout)
        self.model.conv_bn_relu3 = Conv1D_BatchNorm_Relu_Dropout(self.config.input_dim, self.config.input_dim, kernel_size=self.config.kernel_size,
                                                           drop_probability=self.config.dropout)
        self.model.flatten = Flatten()
        self.model.dense = FC_BatchNorm_Relu_Dropout(self.config.input_dim * self.config.sequencelength, 4 * self.config.input_dim, drop_probability=self.config.dropout)
        self.model.logsoftmax = nn.Sequential(nn.Linear(4 * self.config.input_dim, self.config.num_classes), nn.LogSoftmax(dim=-1))

    def forward(self, x):
        # require NxTxD
        x = x.transpose(1,2)
        x = self.model.conv_bn_relu1(x)
        x = self.model.conv_bn_relu2(x)
        x = self.model.conv_bn_relu3(x)
        x = self.model.flatten(x)
        x = self.model.dense(x)
        return self.model.logsoftmax(x)

class Conv1D_BatchNorm_Relu_Dropout(torch.nn.Module):
    def __init__(self, input_dim, hidden_dims, kernel_size=5, drop_probability=0.5):
        super(Conv1D_BatchNorm_Relu_Dropout, self).__init__()

        self.block = nn.Sequential(
            nn.Conv1d(input_dim, hidden_dims, kernel_size, padding=(kernel_size // 2)),
            nn.BatchNorm1d(hidden_dims),
            nn.ReLU(),
            nn.Dropout(p=drop_probability)
        )

    def forward(self, X):
        return self.block(X)


class FC_BatchNorm_Relu_Dropout(torch.nn.Module):
    def __init__(self, input_dim, hidden_dims, drop_probability=0.5):
        super(FC_BatchNorm_Relu_Dropout, self).__init__()

        self.block = nn.Sequential(
            nn.Linear(input_dim, hidden_dims),
            nn.BatchNorm1d(hidden_dims),
            nn.ReLU(),
            nn.Dropout(p=drop_probability)
        )

    def forward(self, X):
        return self.block(X)


class Flatten(nn.Module):
    def forward(self, input):
        return input.view(input.size(0), -1)

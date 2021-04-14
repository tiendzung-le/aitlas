import collections
import logging
import os
from shutil import copyfile

import numpy as np
import torch
import torch.nn as nn
from torch.utils.tensorboard import SummaryWriter
from torchvision import transforms
from tqdm import tqdm

from ..utils import current_ts, get_class, image_loader, stringify
from .config import Configurable
from .datasets import BaseDataset
from .metrics import RunningScore
from .schemas import BaseModelSchema


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class BaseModel(nn.Module, Configurable):

    schema = BaseModelSchema

    def __init__(self, config=None):
        Configurable.__init__(self, config)
        super(BaseModel, self).__init__()

        self.model = nn.Module()

        device_name = "cpu"
        if self.config.use_cuda and torch.cuda.is_available():
            device_name = "cuda"

        self.device = torch.device(device_name)

        self.metrics = self.config.metrics
        self.num_classes = self.config.num_classes
        self.running_metrics = RunningScore(self.num_classes, self.device)
        self.weights = self.config.weights

    def prepare(self):
        """Prepare the model before using it """

        # load loss, optimizer and lr scheduler
        self.criterion = self.load_criterion()
        self.optimizer = self.load_optimizer()
        self.lr_scheduler = self.load_lr_scheduler()

    def fit(
        self,
        dataset: BaseDataset,
        epochs: int = 100,
        model_directory: str = None,
        save_epochs: int = 10,
        iterations_log: int = 100,
        resume_model: str = None,
        val_dataset: BaseDataset = None,
        run_id: str = None,
        **kwargs,
    ):
        logging.info("Starting training.")

        start_epoch = 0
        start = current_ts()

        # load the model if needs to resume training
        if resume_model:
            start_epoch, loss, start, run_id = self.load_model(
                resume_model, self.optimizer
            )

        # allocate device
        self.allocate_device()

        # start logger
        self.writer = SummaryWriter(os.path.join(model_directory, run_id))

        # get data loaders
        train_loader = dataset.dataloader()
        val_loader = None
        if val_dataset:
            val_loader = val_dataset.dataloader()

        for epoch in range(start_epoch, epochs):  # loop over the dataset multiple times
            loss = self.train_epoch(
                epoch, train_loader, self.optimizer, self.criterion, iterations_log
            )
            self.writer.add_scalar("Loss/train", loss, epoch + 1)
            if epoch % save_epochs == 0:
                self.save_model(
                    model_directory, epoch, self.optimizer, loss, start, run_id
                )

            # adjust learning rate if needed
            if self.lr_scheduler:
                self.lr_scheduler.step()

            # evaluate against the train set
            train_loss = self.evaluate_model(
                train_loader,
                criterion=self.criterion,
                description="testing on train set",
            )
            print(f"f1 {self.running_metrics.f1_score()}")
            print(f"p {self.running_metrics.precision()}")
            print(f"r {self.running_metrics.recall()}")
            self.log_metrics(
                self.running_metrics.get_scores(self.metrics),
                dataset.get_labels(),
                "train",
                self.writer,
                epoch + 1,
            )
            self.running_metrics.reset()

            # evaluate against a validation set if there is one
            if val_loader:
                val_loss = self.evaluate_model(
                    val_loader,
                    criterion=self.criterion,
                    description="testing on validation set",
                )
                self.log_metrics(
                    self.running_metrics.get_scores(self.metrics),
                    dataset.get_labels(),
                    "val",
                    self.writer,
                    epoch + 1,
                )
                self.writer.add_scalar("Loss/val", val_loss, epoch + 1)
                self.running_metrics.reset()

        self.writer.close()

        # save the model in the end
        self.save_model(model_directory, epochs, self.optimizer, loss, start, run_id)

        logging.info(f"finished training. training time: {current_ts() - start}")

    def train_epoch(self, epoch, dataloader, optimizer, criterion, iterations_log):
        start = current_ts()
        running_loss = 0.0
        total_loss = 0.0

        self.model.train()
        for i, data in enumerate(tqdm(dataloader, desc="training")):
            # get the inputs; data is a list of [inputs, labels]
            inputs, labels = data
            inputs = inputs.to(self.device)
            labels = labels.to(self.device)

            # zero the parameter gradients
            if isinstance(optimizer, tuple):
                for opt in optimizer:
                    opt.zero_grad()
            else:
                optimizer.zero_grad()

            # forward + backward + optimize
            outputs = self(inputs)

            # check if outputs is OrderedDict for segmentation
            if isinstance(outputs, collections.Mapping):
                outputs = outputs["out"]

            loss = criterion(
                outputs, labels if len(labels.shape) == 1 else labels.type(torch.float)
            )  # TODO: Check this converion OUT!!!
            loss.backward()

            # perform a single optimization step
            if isinstance(optimizer, tuple):
                for opt in optimizer:
                    opt.step()
            else:
                optimizer.step()

            # log statistics
            running_loss += loss.item() * inputs.size(0)
            total_loss += loss.item() * inputs.size(0)

            if (
                i % iterations_log == iterations_log - 1
            ):  # print every iterations_log mini-batches
                logging.info(
                    f"[{epoch + 1}, {i + 1}], loss: {running_loss / iterations_log : .5f}"
                )
                running_loss = 0.0

        total_loss = total_loss / len(dataloader.dataset)
        logging.info(
            f"epoch: {epoch + 1}, time: {current_ts() - start}, loss: {total_loss: .5f}"
        )
        return total_loss

    def evaluate(
        self, dataset: BaseDataset = None, model_path: str = None,
    ):
        """
        Evaluate a model stored in a specified path against a given dataset

        :param dataset: the dataset to evaluate against
        :param model_path: the path to the model on disk
        :return:
        """
        # load the model
        self.load_model(model_path)

        # get test data loader
        dataloader = dataset.dataloader()

        # evaluate model on data
        result = self.evaluate_model(dataloader, description="testing on test set")

        return result

    def evaluate_model(
        self, dataloader, criterion=None, description="testing on validation set",
    ):
        """
        Evaluates the current model against the specified dataloader for the specified metrics
        :param dataloader:
        :param metrics: list of metric keys to calculate
        :criterion: Criterion to calculate loss
        :description: What to show in the progress bar
        :return: tuple of (metrics, y_true, y_pred)
        """
        self.model.eval()

        # initialize loss if applicable
        total_loss = 0.0

        for inputs, outputs, labels in self.predict_output_per_batch(
            dataloader, description
        ):
            if criterion:
                batch_loss = criterion(outputs, labels)
                total_loss += batch_loss.item() * inputs.size(0)

            predicted_probs, predicted = self.get_predicted(outputs)
            # if segmentation reshape the predictions and labels
            if len(predicted.shape) > 2:
                predicted = predicted.T.reshape(
                    predicted.shape[0] * predicted.shape[2] * predicted.shape[3],
                    predicted.shape[1],
                )
                labels = labels.T.reshape(
                    labels.shape[0] * labels.shape[2] * labels.shape[3], labels.shape[1]
                )

            if (
                len(labels.shape) == 1
            ):  # if it is multiclass, then we need one hot encoding for the predictions
                one_hot = torch.zeros(labels.size(0), self.num_classes)
                one_hot[torch.arange(labels.size(0)), predicted.type(torch.long)] = 1
                predicted = one_hot
                predicted = predicted.to(self.device)

            self.running_metrics.update(
                labels.type(torch.uint8), predicted.type(torch.uint8)
            )

        if criterion:
            total_loss = total_loss / len(dataloader.dataset)

        return total_loss

    def predict(
        self, dataset: BaseDataset = None, description="running prediction",
    ):
        """
        Predicts using a model against for a specified dataset

        :return: tuple of (y_true, y_pred, y_pred_probs)
        """
        # initialize counters
        y_true = []
        y_pred = []
        y_pred_probs = []

        # predict
        for inputs, outputs, labels in self.predict_output_per_batch(
            dataset.dataloader(), description
        ):
            predicted_probs, predicted = self.get_predicted(outputs)
            y_pred_probs += list(predicted_probs.cpu().detach().numpy())
            y_pred += list(predicted.cpu().detach().numpy())
            y_true += list(labels.cpu().detach().numpy())

        return y_true, y_pred, y_pred_probs

    def predict_image(
        self,
        image=None,
        data_transforms=None,
        description="running prediction for single image",
    ):
        """
        Predicts using a model against for a specified image

        :return: tuple of (y_true, y_pred, y_pred_probs)
        """
        # load the image and apply transformations, if transforms in None convert only to Tensor
        self.model.eval()
        if data_transforms:
            image = data_transforms(image)
        else:
            data_transforms = transforms.Compose([transforms.ToTensor(),])
            image = data_transforms(image)
        # convert to batch of size 1
        inputs = image.unsqueeze(0).to(self.device)
        outputs = self(inputs)
        # check if outputs is OrderedDict for segmentation
        if isinstance(outputs, collections.Mapping):
            outputs = outputs["out"]

        predicted_probs, predicted = self.get_predicted(outputs)
        y_pred_probs = list(predicted_probs.cpu().detach().numpy())
        y_pred = list(predicted.cpu().detach().numpy())
        y_true = None

        return y_true, y_pred, y_pred_probs

    def predict_output_per_batch(self, dataloader, description):
        """Run predictions on a dataloader and return inputs, outputs, labels per batch"""

        # turn on eval mode
        self.model.eval()

        # run predictions
        with torch.no_grad():
            for i, data in enumerate(tqdm(dataloader, desc=description)):
                inputs, labels = data
                inputs = inputs.to(self.device)
                labels = labels.to(self.device)

                outputs = self(inputs)

                # check if outputs is OrderedDict for segmentation
                if isinstance(outputs, collections.Mapping):
                    outputs = outputs["out"]

                yield inputs, outputs, labels

    def forward(self, *input, **kwargs):
        """
        Abstract method implementing the model. Extending classes should override this method.
        :return:  instance extending `nn.Module`
        """
        raise NotImplementedError

    def get_predicted(self, outputs):
        """Gets the output from the model and return the predictions
        :return: tuple in the format (probabilities, predicted classes/labels)
        """
        raise NotImplementedError("Please implement `get_predicted` for your model. ")

    def report(self, labels, **kwargs):
        """The report we want to generate for the model"""
        return ()

    def log_metrics(self, output, labels, tag="train", writer=None, epoch=0):
        """Log the calculated metrics"""
        calculated_metrics = output
        logging.info(stringify(calculated_metrics))
        if writer:
            for cm in calculated_metrics:
                for key in cm:
                    metric = cm[key]
                    if isinstance(metric, list) or (metric.ndim > 0):
                        for i, sub in enumerate(metric):
                            writer.add_scalar(f"{key}/{labels[i]}/{tag}", sub, epoch)
                    else:
                        writer.add_scalar(f"{key}/{tag}", metric, epoch)

    def allocate_device(self, opts=None):
        """
        Put the model on CPU or GPU
        :return:
        """
        if torch.cuda.device_count() > 1:
            self.model = nn.DataParallel(self.model)
        self.model = self.model.to(self.device)
        return self.model

    def save_model(self, model_directory, epoch, optimizer, loss, start, run_id):
        """
        Saves the model on disk
        :param model_directory:
        :return:
        """
        if not os.path.isdir(model_directory):
            os.makedirs(model_directory)

        if not os.path.isdir(os.path.join(model_directory, run_id)):
            os.makedirs(os.path.join(model_directory, run_id))

        timestamp = current_ts()
        checkpoint = os.path.join(
            model_directory, run_id, f"checkpoint_{timestamp}.pth.tar"
        )

        # create timestamped checkpoint
        torch.save(
            {
                "epoch": epoch + 1,
                "state_dict": self.model.state_dict(),
                "optimizer": optimizer.state_dict(),
                "loss": loss,
                "start": start,
                "id": run_id,
            },
            checkpoint,
        )

        # replace last checkpoint
        copyfile(checkpoint, os.path.join(model_directory, "checkpoint.pth.tar"))

    def extract_features(self, *input, **kwargs):
        """
        Abstract for trim the model to extract feature. Extending classes should override this method.
        """
        return self.model

    def load_model(self, file_path, optimizer=None):
        """Loads a model from a checkpoint"""
        if os.path.isfile(file_path):
            logging.info(f"Loading checkpoint {file_path}")
            checkpoint = torch.load(file_path)

            if "state_dict" in checkpoint:
                self.model.load_state_dict(checkpoint["state_dict"])
                self.allocate_device()

                start_epoch = checkpoint["epoch"]
                loss = checkpoint["loss"]
                start = checkpoint["start"]
                run_id = checkpoint["id"]
            else:
                self.model.load_state_dict(checkpoint)
                self.allocate_device()

                start_epoch = 1
                loss = 0
                start = 0
                run_id = ""

            if optimizer:
                optimizer.load_state_dict(checkpoint["optimizer"])

            logging.info(f"Loaded checkpoint {file_path} at epoch {start_epoch}")
            return (start_epoch, loss, start, run_id)
        else:
            raise ValueError(f"No checkpoint found at {file_path}")

    def load_optimizer(self):
        """Load the optimizer"""
        raise NotImplementedError("Please implement `load_optimizer` for your model. ")

    def load_criterion(self):
        """Load the loss function"""
        raise NotImplementedError("Please implement `load_criterion` for your model. ")

    def load_lr_scheduler(self):
        raise NotImplementedError(
            "Please implement `load_lr_scheduler` for your model. "
        )

    def train_model(
        self,
        train_dataset: BaseDataset,
        epochs: int = 100,
        model_directory: str = None,
        save_epochs: int = 10,
        iterations_log: int = 100,
        resume_model: str = None,
        val_dataset: BaseDataset = None,
        run_id: str = None,
        **kwargs,
    ):
        return self.fit(
            dataset=train_dataset,
            epochs=epochs,
            model_directory=model_directory,
            save_epochs=save_epochs,
            iterations_log=iterations_log,
            resume_model=resume_model,
            run_id=run_id,
            **kwargs,
        )

    def train_and_evaluate_model(
        self,
        train_dataset: BaseDataset,
        epochs: int = 100,
        model_directory: str = None,
        save_epochs: int = 10,
        iterations_log: int = 100,
        resume_model: str = None,
        val_dataset: BaseDataset = None,
        run_id: str = None,
        **kwargs,
    ):
        return self.fit(
            dataset=train_dataset,
            epochs=epochs,
            model_directory=model_directory,
            save_epochs=save_epochs,
            iterations_log=iterations_log,
            resume_model=resume_model,
            val_dataset=val_dataset,
            run_id=run_id,
            **kwargs,
        )

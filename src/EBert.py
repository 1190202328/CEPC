import copy
import gc
import math
import os
import random
import shutil
import sys
import time

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from CEPC.src.EBertUtils import EBertDataset, ETaskState, ETokenAligner, EBalanceBatchMode, \
    EInputListMode, EBertTrainingTools
from CEPC.src.ELib import ELib
from CEPC.src.EModels import EBertClassifier, EBertClassifierDACoordinated
from termcolor import colored
from torch.utils.data import DataLoader
from torch.utils.data.sampler import WeightedRandomSampler
from transformers import BertTokenizer


class EBertCLSType:
    none = 0
    coordinated = 11


class EBert:

    def __init__(self, config, sync_obj=None, **kwargs):
        # general properties
        self.config = config
        self.model_id = 0
        self.current_train_epoch = -1
        self.scheduler_overall_steps = -1
        self.early_stopped_epoch = -1
        self.train_loss_early_stopped_epoch = -1
        self.sync_obj = sync_obj
        self.delay_optimizer = False
        self.delay_optimizer_loss = 0.0
        self.custom_batch_sampler_class = None
        self.custom_train_loss_func = None
        self.custom_test_loss_func = None
        self.custom_gradient_modifier_func = None
        self.removed_modules = list()
        self.init_seed(self.config.seed)
        # cls settings
        if self.config.cls_type == EBertCLSType.coordinated:
            self.bert_classifier = EBertClassifier.create(EBertClassifierDACoordinated, self, self.config, **kwargs)
        else:
            self.bert_classifier = None
        self.tokenizer = BertTokenizer.from_pretrained(self.config.model_path)
        ELib.PASS()

    def init_seed(self, seed):
        ### version 1
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)
            torch.backends.cudnn.enabled = False
            torch.backends.cudnn.benchmark = False
            torch.backends.cudnn.deterministic = True
        # ### version 0
        # np.random.seed(seed)
        # torch.manual_seed(seed)
        # if torch.cuda.is_available():
        #     torch.cuda.manual_seed_all(seed)

    def sleep(self):
        time.sleep(0.01)

    def __loader_batches(self, dataset_list, config, shuffle, drop_last, input_mode, is_test, balance_batch_mode_list):
        batch_groups = list()
        min_batch_count = sys.maxsize
        max_batch_count = 0
        for da_ind, cur_dataset in enumerate(dataset_list):
            if (is_test) or (self.sync_obj is None) or (da_ind not in self.sync_obj.sync_bundle_batches):
                ## create batches
                cur_loader = None
                if shuffle:
                    if self.custom_batch_sampler_class is None:
                        sample_weights = []
                        for d_ind, cur_input_y in enumerate(cur_dataset.input_bundle.input_y):
                            if balance_batch_mode_list[da_ind] == EBalanceBatchMode.label_based:
                                labels = cur_input_y
                                if config.train_by_log_softmax and config.training_log_softmax_weight == 1:
                                    cur_input_y_row = cur_dataset.input_bundle.input_y_row[d_ind]
                                    labels = [entry.index(max(entry)) for entry in cur_input_y_row]
                                lbl_values = set(labels)
                                class_count = dict()
                                for cur_lbl in lbl_values:
                                    class_count[cur_lbl] = labels.count(cur_lbl)
                                cur_sample_weights = [(1 / len(class_count)) / class_count[entry] for entry in labels]
                            elif balance_batch_mode_list[
                                da_ind] == EBalanceBatchMode.meta_based_discrete:  # was custom_based
                                meta_values = set(cur_dataset.input_bundle.input_meta)
                                class_count = dict()
                                for cur_meta in meta_values:
                                    class_count[cur_meta] = cur_dataset.input_bundle.input_meta.count(cur_meta)
                                cur_sample_weights = [(1 / len(class_count)) / class_count[entry] for entry in
                                                      cur_dataset.input_bundle.input_meta]
                                ELib.PASS()
                            elif balance_batch_mode_list[da_ind] == EBalanceBatchMode.meta_based_continuous:
                                cur_sample_weights = copy.deepcopy(cur_dataset.input_bundle.input_meta)
                                w_sum = sum(cur_sample_weights)
                                cur_sample_weights = [entry / w_sum for entry in cur_sample_weights]
                            else:
                                cur_sample_weights = [(1 / len(cur_input_y)) for entry in cur_input_y]
                            sample_weights.append(cur_sample_weights)
                        sample_weights_mean = ELib.average_lists_elementwise(sample_weights)
                        sampler = WeightedRandomSampler(sample_weights_mean, len(sample_weights_mean), True)
                        cur_loader = DataLoader(dataset=cur_dataset, batch_size=config.batch_size,
                                                drop_last=drop_last, sampler=sampler, num_workers=0)
                    else:
                        batch_sampler = self.custom_batch_sampler_class(cur_dataset, config.batch_size,
                                                                        len(cur_dataset))
                        cur_loader = DataLoader(dataset=cur_dataset, batch_sampler=batch_sampler, num_workers=0)
                else:
                    cur_loader = DataLoader(dataset=cur_dataset, batch_size=config.batch_size, shuffle=False,
                                            drop_last=drop_last, num_workers=0)
                ## collect batches
                batch_groups.append(list())
                for cur_batch in cur_loader:
                    batch_groups[-1].append(cur_batch)
                ## store the list of batches if needed
                if (not is_test) and (self.sync_obj is not None) and \
                        (self.sync_obj.sync_bundle_indices is None or da_ind in self.sync_obj.sync_bundle_indices):
                    self.sync_obj.sync_bundle_batches[da_ind] = batch_groups[-1]
            else:
                ## restore the list of batches if they are created by another model
                batch_groups.append(list())
                batch_groups[-1] = copy.deepcopy(self.sync_obj.sync_bundle_batches[da_ind])
            min_batch_count = min(min_batch_count, len(batch_groups[-1]))
            max_batch_count = max(max_batch_count, len(batch_groups[-1]))
        ## check if the number of batches between the models are aligned
        if (not is_test) and (self.sync_obj is not None):
            self.sync_obj.sync_bundle_batches_sizes.append([min_batch_count, max_batch_count])
            if not self.sync_obj.verify_synced_batch_sizes():
                print(colored('align the length of the bundles in the synced models!', 'red'))
                sys.exit(1)
        ## sequentialize or parallelize the batches
        result = list()
        if input_mode == EInputListMode.sequential:
            for cur_batch_ind in range(max_batch_count):
                for cur_group in batch_groups:
                    if cur_batch_ind < len(cur_group):
                        result.append(cur_group[cur_batch_ind])
        elif input_mode == EInputListMode.parallel:
            for cur_batch_ind in range(min_batch_count):
                cur_batches = dict()
                for group_ind, cur_group in enumerate(batch_groups):
                    cur_batches[group_ind] = cur_group[cur_batch_ind]
                result.append(cur_batches)
        elif input_mode == EInputListMode.parallel_full:
            for cur_batch_ind in range(max_batch_count):
                cur_batches = dict()
                for group_ind, cur_group in enumerate(batch_groups):
                    if cur_batch_ind < len(cur_group):
                        cur_batches[group_ind] = cur_group[cur_batch_ind]
                    else:
                        cur_batches[group_ind] = dict()
                result.append(cur_batches)
        return result

    def generate_batches(self, dataset_list, config, shuffle, drop_last, epoch_index, input_mode,
                         balance_batch_mode_list=None):
        b_modes = list()
        if balance_batch_mode_list is None:
            for _ in dataset_list:
                b_modes.append(self.config.balance_batch_mode)
        else:
            b_modes = balance_batch_mode_list
        is_test = (not shuffle) or (not drop_last)
        ## create batches (ignore syncing if it is not training)
        if is_test or self.sync_obj is None:
            batches = self.__loader_batches(dataset_list, config, shuffle, drop_last, input_mode, is_test, b_modes)
        else:
            with self.sync_obj.lock_dataset:
                if self.sync_obj.sync_counter == self.sync_obj.model_count:
                    self.sync_obj.reset()
                    self.init_seed(self.sync_obj.seed * (epoch_index + 10))  # not need it anymore, but will leave it
                    batches = self.__loader_batches(dataset_list, config, shuffle, drop_last, input_mode, is_test,
                                                    b_modes)
                else:
                    batches = self.__loader_batches(dataset_list, config, shuffle, drop_last, input_mode, is_test,
                                                    b_modes)
                self.sync_obj.sync_counter += 1
            while self.sync_obj.sync_counter < self.sync_obj.model_count:
                self.sleep()
        ## move them to gpu and return
        for cur_batch in batches:
            result_dict = self.__move_batch_to_gpu(cur_batch, config.device, input_mode)
            result_dict['batch_count'] = len(batches)
            yield result_dict

    def __move_batch_to_gpu(self, batch, device, input_mode):
        result = dict()
        if input_mode == EInputListMode.sequential:
            for name, item in batch.items():
                if type(item) is torch.Tensor:
                    result[name] = item.to(device)  # takes a copy and moves it to the device
                else:
                    result[name] = item
        elif input_mode == EInputListMode.parallel or input_mode == EInputListMode.parallel_full:
            for b_k, sub_batch in batch.items():
                if type(sub_batch) is dict:
                    result[b_k] = dict()
                    for name, item in sub_batch.items():
                        if type(item) is torch.Tensor:
                            result[b_k][name] = item.to(device)  # takes a copy and moves it to the device
                        else:
                            result[b_k][name] = item
                else:
                    result[b_k] = sub_batch
        return result

    def delete_batch_from_gpu(self, batch, input_mode):
        if input_mode == EInputListMode.sequential:
            for k, v in batch.items():
                if type(v) is torch.Tensor:
                    del v  # delete the tensor from the gpu
        elif input_mode == EInputListMode.parallel or input_mode == EInputListMode.parallel_full:
            for b_k, sub_batch in batch.items():
                if type(sub_batch) is dict:
                    for k, v in sub_batch.items():
                        if type(v) is torch.Tensor:
                            del v  # delete the tensor from the gpu
                    del sub_batch
        del batch

    def set_module_learning_rate(self, module, lr):
        if isinstance(module, nn.ModuleList):
            print(colored('>>> cannot handle ModuleList to set the LR in the optimizer! <<<', 'red'))
            sys.exit(1)
        params = list(module.parameters())
        found = None
        for p_ind, cur_group in enumerate(self.optimizer.param_groups):
            try:
                if params[0] in cur_group['params']:
                    found = cur_group
                    break
            except:
                pass
        if found is not None:
            found['lr'] = lr
            found['initial_lr'] = lr
            self.scheduler.base_lrs[p_ind] = lr
        else:
            print(colored('>>> module was not found to set the LR in the optimizer! <<< \n'
                          'perhaps you have passed "customized_params" to setup_optimizer()', 'red'))
            sys.exit(1)
        ELib.PASS()

    def remove_module_from_optimizer(self, module):
        if isinstance(module, nn.ModuleList):
            print(colored('>>> cannot handle ModuleList to delete from the optimizer! <<<', 'red'))
            sys.exit(1)
        params = list(module.parameters())
        found = None
        for cur_group in self.optimizer.param_groups:
            try:
                if params[0] in cur_group['params']:
                    found = cur_group
                    break
            except:
                pass
        if found is not None:
            self.removed_modules.append([module, found['lr'], found['weight_decay'], found['eps']])
            self.optimizer.param_groups.remove(found)
        else:
            print(colored('>>> module was not found for deletion in the optimizer! <<< \n'
                          'perhaps you have passed "customized_params" to setup_optimizer()', 'red'))
            sys.exit(1)
        ELib.PASS()

    def add_module_to_optimizer(self, module):
        if isinstance(module, nn.ModuleList):
            print(colored('>>> cannot handle ModuleList to add to the optimizer! <<<', 'red'))
            sys.exit(1)
        lr = self.config.learn_rate
        weight_decay = self.config.weight_decay
        eps = self.config.adam_epsilon
        for cur_module_info in self.removed_modules:
            if cur_module_info[0] == module:
                lr = cur_module_info[1]
                weight_decay = cur_module_info[2]
                eps = cur_module_info[3]
                self.removed_modules.remove(cur_module_info)
                break
        self.optimizer.add_param_group(
            {
                'params': module.parameters(),
                'lr': lr,
                'weight_decay': weight_decay,
                'eps': eps
            }
        )
        ELib.PASS()

    def setup_optimizer(self, customized_params=None):
        if customized_params is None:
            self.optimizer = EBertTrainingTools.get_optimizer_by_modules(self.bert_classifier,
                                                                         self.config.learn_rate,
                                                                         self.config.weight_decay,
                                                                         self.config.adam_epsilon)
        else:
            self.optimizer = EBertTrainingTools.get_optimizer(customized_params,
                                                              self.config.learn_rate,
                                                              self.config.weight_decay,
                                                              self.config.adam_epsilon)

    def setup_scheduler(self, train_bundle_list, input_mode, extra_trainset_size=0, extra_epochs=0):
        self.scheduler = EBertTrainingTools.get_scheduler(self.optimizer,
                                                          self.config.epoch_count,
                                                          self.config.batch_size,
                                                          self.config.warmup_steps,
                                                          train_bundle_list,
                                                          input_mode,
                                                          extra_trainset_size, extra_epochs)
        self.scheduler_overall_steps = EBertTrainingTools.get_training_steps(self.config.epoch_count,
                                                                             self.config.batch_size,
                                                                             train_bundle_list,
                                                                             input_mode,
                                                                             extra_trainset_size, extra_epochs)

    def setup_objective(self, weighted_instance_loss):
        # weights = list()
        # max_lbl = max(train_bundle.input_y)
        # for cur_lbl in range(0, max_lbl + 1):
        #     weights.append(train_bundle.input_y.count(cur_lbl))
        # weights = [entry / len(train_bundle.input_y) for entry in weights]
        # weights = [1 - entry for entry in weights]
        # self.loss_func = nn.CrossEntropyLoss(torch.tensor(weights).to(self.config.device))
        if weighted_instance_loss:
            self.loss_func = nn.CrossEntropyLoss(reduction='none')
        else:
            self.loss_func = nn.CrossEntropyLoss()

    def __setup_learning_tools(self, train_bundle_list, weighted_instance_loss, input_mode,
                               extra_scheduled_trainset_size=0, extra_scheduled_epochs=0,
                               customized_optimizer_params=None):
        self.setup_optimizer(customized_optimizer_params)
        self.setup_scheduler(train_bundle_list, input_mode,
                             extra_scheduled_trainset_size, extra_scheduled_epochs)
        self.setup_objective(weighted_instance_loss)

    def back_prop_and_zero_grad(self, loss, clip_grads=True, apply_scheduler=True):
        loss.backward()
        if not self.delay_optimizer:
            if self.custom_gradient_modifier_func is not None:
                self.custom_gradient_modifier_func(self)
            if clip_grads:
                torch.nn.utils.clip_grad_norm_(self.bert_classifier.parameters(), self.config.max_grad_norm)
            self.optimizer.step()
            if apply_scheduler:
                self.scheduler.step()
            self.bert_classifier.zero_grad()

    def __process_loss(self, outcome, batch, task_dic, do_train, weighted_instance_loss):
        total_loss = 0
        if do_train:
            if self.custom_train_loss_func is not None:
                ## if it is training and there is a custom_loss, then calculate it
                total_loss, task_name, model_pred, ground_truth = self.custom_train_loss_func(self, outcome, batch)
                if not self.delay_optimizer:
                    self.delay_optimizer_loss += total_loss.item()
                    task_dic[task_name].update(b_size=model_pred.size()[0],
                                               b_loss=model_pred.size()[0] * self.delay_optimizer_loss,
                                               b_lbl_true=ground_truth.to('cpu').detach().numpy().tolist(),
                                               b_lbl_pred=model_pred.to('cpu').detach().argmax(dim=1).numpy().tolist(),
                                               b_logits=model_pred.to('cpu').detach().numpy().tolist())
                    self.delay_optimizer_loss = 0.0
                else:
                    self.delay_optimizer_loss += total_loss.item()
            else:
                ## if it is training and there is no custom_loss,
                ## then depending on the settings compute the loss per task
                for cur_pred in outcome:
                    ## distinguish sequential batches from parallels
                    if 'task_list' in batch:
                        cur_batch = batch
                    else:
                        for b_k, sub_batch in batch.items():
                            if type(sub_batch) is dict:
                                for ta_name in sub_batch['task_list']:
                                    if cur_pred[0] == ta_name[0]:
                                        cur_batch = sub_batch
                                        break
                    ## calculate loss
                    y_ind = 0
                    for y_ind, ta_name in enumerate(cur_batch['task_list']):
                        if cur_pred[0] == ta_name[0]:
                            break
                    y_ref = 'y_' + str(y_ind)
                    y_row_ref = 'y_row_' + str(y_ind)
                    if self.config.train_by_log_softmax:
                        pred_softmaxed = F.log_softmax(cur_pred[1] / self.config.training_softmax_temperature, dim=1)
                        if self.config.training_log_softmax_weight == 1:
                            batch_loss = pow(self.config.training_softmax_temperature, 2) * \
                                         -(cur_batch[y_row_ref] * pred_softmaxed).sum(dim=1)
                            if not weighted_instance_loss:
                                batch_loss = batch_loss.mean()
                        else:
                            if not weighted_instance_loss:
                                batch_loss = pow(self.config.training_softmax_temperature, 2) * \
                                             self.config.training_log_softmax_weight * \
                                             (-(cur_batch[y_row_ref] * pred_softmaxed).sum(dim=1)).mean() + \
                                             (1 - self.config.training_log_softmax_weight) * \
                                             self.loss_func(cur_pred[1], cur_batch[y_ref])
                            else:
                                batch_loss = pow(self.config.training_softmax_temperature, 2) * \
                                             self.config.training_log_softmax_weight * \
                                             (-(cur_batch[y_row_ref] * pred_softmaxed).sum(dim=1)) + \
                                             (1 - self.config.training_log_softmax_weight) * \
                                             self.loss_func(cur_pred[1], cur_batch[y_ref])
                    else:
                        batch_loss = self.loss_func(cur_pred[1], cur_batch[y_ref])
                    if weighted_instance_loss:
                        batch_loss = batch_loss * cur_batch['weight']
                        batch_loss = batch_loss.mean()
                    total_loss += batch_loss
                    task_name = cur_pred[0]
                    task_dic[task_name].update(b_size=cur_pred[1].size()[0],
                                               b_loss=cur_pred[1].size()[0] * batch_loss.item(),
                                               b_lbl_true=cur_batch[y_ref].to('cpu').detach().numpy().tolist(),
                                               b_lbl_pred=cur_pred[1].to('cpu').detach().argmax(dim=1).numpy().tolist(),
                                               b_logits=cur_pred[1].to('cpu').detach().numpy().tolist())
        else:
            if self.custom_test_loss_func is not None:
                ## if it is testing and there is a custom_loss, then calculate it
                total_loss, task_name, model_pred, ground_truth = self.custom_test_loss_func(self, outcome, batch)
                task_dic[task_name].update(b_size=model_pred.size()[0],
                                           b_loss=model_pred.size()[0] * total_loss.item(),
                                           b_lbl_true=ground_truth.to('cpu').detach().numpy().tolist(),
                                           b_lbl_pred=model_pred.to('cpu').detach().argmax(dim=1).numpy().tolist(),
                                           b_logits=model_pred.to('cpu').detach().numpy().tolist())
            else:
                ## if it is testing and there is no custom_loss, then collect the results per task
                for cur_pred in outcome:
                    y_ind = 0
                    for y_ind, ta_name in enumerate(batch['task_list']):
                        if cur_pred[0] == ta_name[0]:
                            break
                    y_ref = 'y_' + str(y_ind)
                    y_row_ref = 'y_row_' + str(y_ind)
                    batch_loss = self.loss_func(cur_pred[1], batch[y_ref])
                    if weighted_instance_loss:
                        batch_loss = batch_loss * batch['weight']
                        batch_loss = batch_loss.mean()
                    total_loss += batch_loss
                    task_name = cur_pred[0]
                    task_dic[task_name].update(b_size=cur_pred[1].size()[0],
                                               b_loss=cur_pred[1].size()[0] * batch_loss.item(),
                                               b_lbl_true=batch[y_ref].to('cpu').detach().numpy().tolist(),
                                               b_lbl_pred=cur_pred[1].to('cpu').detach().argmax(dim=1).numpy().tolist(),
                                               b_logits=cur_pred[1].to('cpu').detach().numpy().tolist())
        ## if it is training, then back-propagate
        if do_train:
            self.back_prop_and_zero_grad(total_loss)
        ELib.PASS()

    def __print_epoch_results(self, ep_no, all_ep, train_tasks, valid_tasks):
        result = 'epoch: {}/{}> '.format(ep_no, all_ep)
        for cur_task in train_tasks.items():
            if cur_task[1].size > 0:
                result += '|T: {}, tr-loss: {:.3f}, tr-f1: {:.3f} '.format(
                    cur_task[0], cur_task[1].loss, cur_task[1].f1)
        if valid_tasks is not None:
            for cur_task in valid_tasks.items():
                if cur_task[1].size > 0:
                    result += '|T: {}, va-loss: {:.3f}, va-f1: {:.3f} '.format(
                        cur_task[0], cur_task[1].loss, cur_task[1].f1)
        result += '\t' + ELib.get_time()
        print(result)

    def __get_dataset_bundle_list(self, bundle_list):
        result = list()
        for cur_bund in bundle_list:
            cur_dt = EBertDataset(cur_bund, self.tokenizer, self.config.max_seq)
            result.append(cur_dt)
        return result

    def get_datasets_and_tasks(self, bundle_list, early_stopping_patience=None):
        if bundle_list is None:
            return None, None
        dt_list = self.__get_dataset_bundle_list(bundle_list)
        tasks = {task: ETaskState(task, early_stopping_patience)
                 for cur_bundle in bundle_list
                 for task in cur_bundle.task_list}
        return dt_list, tasks

    def __train_one_epoch(self, train_dt_list, train_tasks, input_mode, weighted_instance_loss,
                          report_number_of_intervals, train_shuffle, train_drop_last, balance_batch_mode_list):
        batches = self.generate_batches(train_dt_list, self.config, train_shuffle, train_drop_last,
                                        self.current_train_epoch, input_mode, balance_batch_mode_list)
        [cur_task[1].reset() for cur_task in train_tasks.items()]
        for ba_ind, cur_batch in enumerate(batches):
            self.bert_classifier.train_step += 1  # to track the overall number inside the classifier
            while True:
                # print(f'cur_batch.keys() = {cur_batch.keys()}')
                # # cur_batch.keys() = dict_keys([0, 1, 2, 3, 'batch_count'])
                # for key in cur_batch:
                #     if key != 'batch_count':
                #         print(f'cur_batch[{key}].keys() = {cur_batch[key].keys()}')
                #     # cur_batch[0].keys() = dict_keys(['x', 'type', 'mask', 'query', 'weight', 'meta', 'docid', 'len', 'task_list', 'y_0', 'y_row_0'])

                # for key in cur_batch[0]:
                #     try:
                #         print(f'cur_batch[0][{key}].shape = {cur_batch[0][key].shape}')
                #     except:
                #         print(f'cur_batch[0][{key}].len = {len(cur_batch[0][key])}')
                # raise Exception
                # # cur_batch[0][x].shape = torch.Size([50, 160])       # 样本，[B, L]
                # # cur_batch[0][type].shape = torch.Size([50, 160])    # [B, L]
                # # cur_batch[0][mask].shape = torch.Size([50, 160])    # [B, L]
                # # cur_batch[0][query].shape = torch.Size([50, 160])   # [B, L]
                # # cur_batch[0][weight].shape = torch.Size([50])       # [B]
                # # cur_batch[0][meta].shape = torch.Size([50])         # [B]
                # # cur_batch[0][docid].len = 50                        # [B]
                # # cur_batch[0][len].shape = torch.Size([50])          # [B]
                # # cur_batch[0][task_list].len = 1                     # ?
                # # cur_batch[0][y_0].shape = torch.Size([50])          # [B]
                # # cur_batch[0][y_row_0].shape = torch.Size([50, 2])   # [B, 2], one_hot编码的标签

                outcome = self.bert_classifier(cur_batch, False)

                # print(outcome.keys())
                # for key in outcome:
                #     for i, item in enumerate(outcome[key]):
                #         print(f'outcome[{key}][{i}].shape = {item.shape}')
                # # outcome[src_output][0].shape = torch.Size([50, 2])  [B, num_class]
                # # outcome[src_output][1].shape = torch.Size([50, 2])
                # # outcome[src_output][2].shape = torch.Size([50, 2])
                # 输出三个域的样本的分类结果

                # print(f'self.custom_train_loss_func = {self.custom_train_loss_func}')
                # # self.custom_train_loss_func = <function EDomainAdaptMine1.__train_loss at 0x7f42de041dc0>

                self.__process_loss(outcome, cur_batch, train_tasks, True, weighted_instance_loss)
                if not self.delay_optimizer:
                    break
            if ELib.progress_made(ba_ind, cur_batch['batch_count'], report_number_of_intervals):
                print(ELib.progress_percent(ba_ind, cur_batch['batch_count']), end=' ', flush=True)
            self.delete_batch_from_gpu(cur_batch, input_mode)
            del cur_batch, outcome
            ## in case there are multiple models and their losses are heavy (in terms of memory)
            ## you can call 'self.sync_obj.lock_loss_calculation.acquire()' in 'self.custom_train_loss_func()'
            ## This way the losses are calculated one by one and after that the models are re-synched
            if self.sync_obj is not None and self.sync_obj.lock_loss_calculation.locked():
                ## wait for the other models to arrive
                if self.sync_obj.sync_counter == self.sync_obj.model_count:
                    self.sync_obj.reset()
                self.sync_obj.sync_counter += 1
                self.sync_obj.lock_loss_calculation.release()
                while self.sync_obj.sync_counter < self.sync_obj.model_count:
                    self.sleep()
            # pprint(vars(self))
            # ELib.PASS()
        ## if there are multiple models avoid double printing the newline
        if self.sync_obj is None:
            print()
        elif self.model_id == 0:
            print()
        ## calculate the metric averages in the epoch
        for cur_task in train_tasks.items():
            if cur_task[1].size > 0:
                cur_task[1].loss /= cur_task[1].size
                cur_task[1].f1 = ELib.calculate_f1(cur_task[1].lbl_true, cur_task[1].lbl_pred)
        ELib.PASS()

    def __validate_one_epoch(self, valid_bundle_list, valid_dt_list, valid_tasks, weighted_instance_loss):
        if valid_bundle_list is not None:
            for cur_bundle in valid_bundle_list:
                if len(cur_bundle.tws) >= 10000:
                    print('Validation set is large it was skipped.', end=' ', flush=True)
                    return None
        # batch_size_copy = self.config.batch_size
        # self.config.batch_size = 512
        stopping_valid_task = None
        if valid_bundle_list is not None:
            self.bert_classifier.eval()
            [cur_task[1].reset() for cur_task in valid_tasks.items()]
            for dt_ind, cur_dt in enumerate(valid_dt_list):
                batches = self.generate_batches([cur_dt], self.config, False, False, self.current_train_epoch,
                                                EInputListMode.sequential)
                for ba_ind, cur_batch in enumerate(batches):
                    outcome = self.bert_classifier(cur_batch, False)
                    self.__process_loss(outcome, cur_batch, valid_tasks, False, weighted_instance_loss)
                    self.delete_batch_from_gpu(cur_batch, EInputListMode.sequential)
                    del cur_batch, outcome
            for cur_task in valid_tasks.items():
                cur_task[1].loss /= cur_task[1].size
                cur_task[1].f1 = ELib.calculate_f1(cur_task[1].lbl_true, cur_task[1].lbl_pred)
            ################ checks early stopping only if the model does not have hooks
            ## deepcopy() cannot copy hooks! fix it later...
            if self.config.check_early_stopping and len(self.bert_classifier.logs) == 0:
                for cur_task in valid_tasks.items():
                    if cur_task[1].learning_state.should_stop(
                            cur_task[1].loss, self.bert_classifier, self.config.device):
                        self.bert_classifier.cpu()
                        self.bert_classifier = cur_task[1].learning_state.best_model
                        stopping_valid_task = cur_task
                        break
        # self.config.batch_size = batch_size_copy
        return stopping_valid_task

    def train(self, train_bundle_list, valid_bundle_list=None, weighted_instance_loss=False,
              input_mode=EInputListMode.sequential, setup_learning_tools=True,
              extra_scheduled_trainset_size=0, extra_scheduled_epochs=0, customized_optimizer_params=None,
              report_number_of_intervals=20, switch_on_train_mode=True, train_shuffle=True, train_drop_last=True,
              balance_batch_mode_list=None, minimum_train_loss=None):
        if len(train_bundle_list) == 1 and len(train_bundle_list[0].tws) < self.config.batch_size:
            return
        ## init
        self.bert_classifier.to(self.config.device)
        self.bert_classifier.zero_grad()
        if setup_learning_tools:
            ## caveat: if you have called train() before this will reset the learning rate and the scheduler!
            self.__setup_learning_tools(train_bundle_list, weighted_instance_loss, input_mode,
                                        extra_scheduled_trainset_size, extra_scheduled_epochs,
                                        customized_optimizer_params)
        train_dt_list, train_tasks = self.get_datasets_and_tasks(train_bundle_list)
        valid_dt_list, valid_tasks = self.get_datasets_and_tasks(valid_bundle_list, self.config.early_stopping_patience)
        ## main loop
        self.early_stopped_epoch = -1
        self.train_loss_early_stopped_epoch = -1
        for cur_ep in range(math.ceil(self.config.epoch_count)):
            self.current_train_epoch = cur_ep
            self.bert_classifier.epoch_index += 1  # to track the overall number inside the classifier
            ## train
            if switch_on_train_mode:
                self.bert_classifier.train()
            else:
                self.bert_classifier.eval()
            self.__train_one_epoch(train_dt_list, train_tasks, input_mode, weighted_instance_loss,
                                   report_number_of_intervals, train_shuffle, train_drop_last, balance_batch_mode_list)
            ## validation
            with torch.no_grad():
                stopping_valid_task = self.__validate_one_epoch(valid_bundle_list, valid_dt_list,
                                                                valid_tasks, weighted_instance_loss)
            ## post process epoch
            self.__print_epoch_results(cur_ep + 1, self.config.epoch_count, train_tasks, valid_tasks)
            if self.config.take_train_checkpoints and (cur_ep + 1) % self.config.train_checkpoint_interval == 0:
                print('saving checkpoint...')
                self.save(str(cur_ep + 1))
            if stopping_valid_task is not None:
                print('stopped early by \'' + stopping_valid_task[0] + '\'. restored the model of epoch {}'.
                      format(stopping_valid_task[1].learning_state.best_index + 1))
                self.early_stopped_epoch = stopping_valid_task[1].learning_state.best_index + 1
                break
            if minimum_train_loss is not None:
                for cur_task in train_tasks.items():
                    if cur_task[1].size > 0 and cur_task[1].loss <= minimum_train_loss:
                        self.train_loss_early_stopped_epoch = cur_ep
                        break
            if self.train_loss_early_stopped_epoch >= 0:
                break
            gc.collect()
            ELib.PASS()
        ## save it if needed
        if self.config.take_train_checkpoints and (cur_ep + 1) % self.config.train_checkpoint_interval != 0:
            print('saving checkpoint...')
            self.save(str(cur_ep + 1))
        self.bert_classifier.cpu()
        ELib.PASS()
        return train_tasks, valid_tasks

    def test(self, test_bundle, return_output_vecs=False, weighted_instance_loss=False,
             print_perf=True, title=None, report_number_of_intervals=20, return_output_vecs_get_details=True):
        if len(test_bundle.task_list) > 1:
            print('only one task is allowed for testing')
            return None
        if len(test_bundle.tws) == 0:
            return list(), list(), list(), list()
        # batch_size_copy = self.config.batch_size
        # self.config.batch_size = 512
        if title is None:
            title = ''
        else:
            title += ' '
        self.bert_classifier.to(self.config.device)
        self.bert_classifier.zero_grad()
        self.bert_classifier.eval()
        self.setup_objective(weighted_instance_loss)
        test_dt = EBertDataset(test_bundle, self.tokenizer, self.config.max_seq)
        batches = self.generate_batches([test_dt], self.config, False, False, 0, EInputListMode.sequential)
        result_vecs = list()
        result_vecs_detail = list()
        tasks = {test_bundle.task_list[0]: ETaskState(test_bundle.task_list[0])}
        print(title + 'labeling ', end=' ', flush=True)
        with torch.no_grad():
            for ba_ind, cur_batch in enumerate(batches):
                outcome = self.bert_classifier(cur_batch, False)
                self.__process_loss(outcome, cur_batch, tasks, False, weighted_instance_loss)
                if return_output_vecs:
                    result_vecs.extend(self.bert_classifier.output_vecs)
                    if self.bert_classifier.output_vecs_detail is not None and return_output_vecs_get_details:
                        result_vecs_detail.extend(self.bert_classifier.output_vecs_detail)
                if ELib.progress_made(ba_ind, cur_batch['batch_count'], report_number_of_intervals):
                    print(ELib.progress_percent(ba_ind, cur_batch['batch_count']), end=' ', flush=True)
                self.delete_batch_from_gpu(cur_batch, EInputListMode.sequential)
                del cur_batch, outcome
        print()
        task_out = tasks[test_bundle.task_list[0]]
        task_out.loss /= task_out.size
        perf = ELib.calculate_metrics(task_out.lbl_true, task_out.lbl_pred)
        if print_perf:
            print('Test Results L1> Loss: {:.3f} F1: {:.3f} Pre: {:.3f} Rec: {:.3f} Acc: {:.3f}'.format(
                task_out.loss, perf[0], perf[1], perf[2], perf[3]) + '\t\t' + ELib.get_time())
        self.bert_classifier.cpu()
        # self.config.batch_size = batch_size_copy
        return task_out.lbl_pred, task_out.logits, [result_vecs, result_vecs_detail], perf

    def save(self, prefix=''):
        torch.save(self.bert_classifier.state_dict(),
                   os.path.join(self.config.output_dir, prefix + 'pytorch_model.bin'))
        if os.path.join(self.config.model_path, 'config.json') != \
                os.path.join(self.config.output_dir, prefix + 'config.json'):
            shutil.copyfile(os.path.join(self.config.model_path, 'config.json'),
                            os.path.join(self.config.output_dir, prefix + 'config.json'))
        if os.path.join(self.config.model_path, 'vocab.txt') != \
                os.path.join(self.config.output_dir, prefix + 'vocab.txt'):
            shutil.copyfile(os.path.join(self.config.model_path, 'vocab.txt'),
                            os.path.join(self.config.output_dir, prefix + 'vocab.txt'))
        ELib.PASS()

    def test_and_save(self, output_dir, file_name, get_vecs, test_bundle, screen_title=None):
        result_lbl, result_logit, result_vecs, perf = self.test(test_bundle, get_vecs, title=screen_title)
        with open(os.path.join(output_dir, file_name), 'w') as ptr:
            for line in result_lbl:
                ptr.write(str(line) + '\n')
        with open(os.path.join(output_dir, file_name + '.h'), 'w') as ptr:  # Human readable labels
            for ind, line in enumerate(result_lbl):
                ptr.write(test_bundle.tws[ind].Tweetid + '\t' + test_bundle.tws[ind].Userid +
                          '\t' + str(line) + '\n')
        with open(os.path.join(output_dir, file_name + '.l'), 'w') as ptr:  # Logits
            for line in result_logit:
                ptr.write(' '.join(list(map(str, line))) + '\n')
        if len(result_vecs[0]) > 0:
            with open(os.path.join(output_dir, file_name + '.v'), 'w') as ptr:  # Sentence Vectors
                for line in result_vecs[0]:
                    ptr.write(' '.join(map(lambda param: '{:.5f}'.format(param), line)) + '\n')
            if len(result_vecs[1]) > 0:
                with open(os.path.join(output_dir, file_name + '.vd'), 'w') as ptr:  # Word Details
                    for tw_ind, tw in enumerate(test_bundle.tws):
                        ptr.write(str(tw) + '\n')
                        cls_line = result_vecs[0][tw_ind]
                        ptr.write('[CLS] ' + ' '.join(map(lambda param: '{:.5f}'.format(param), cls_line)) + '\n')
                        bert_tokens = self.tokenizer.tokenize(test_bundle.input_x[tw_ind])
                        align_ind = ETokenAligner.align(bert_tokens, tw.ETokens)
                        vec_detail = result_vecs[1][tw_ind]
                        vec_detail = vec_detail[1:-1]
                        for tok_ind, cur_tok in enumerate(tw.ETokens):
                            text = cur_tok.Text
                            vec = vec_detail[align_ind[tok_ind]]
                            if cur_tok.POS == 'U':
                                text = 'www'
                            ptr.write(text + ' ' + ' '.join(map(lambda param: '{:.5f}'.format(param), vec)) + '\n')
                        ptr.write('\n')
        return perf

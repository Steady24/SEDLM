#!/usr/bin/env python
# -*- coding: utf-8 -*-

from time import time
from copy import deepcopy

from torch import no_grad, cat, zeros
from torch.optim import Adam
from torch.nn import BCEWithLogitsLoss, utils
from torch.cuda import is_available

from tools.metrics import f1_per_frame, error_rate_per_frame
from tools.printing import print_msg, inform_about_device, \
    InformAboutProcess, print_evaluation_results, \
    print_training_results
from data_feeders import get_tut_sed_data_loader

__author__ = 'Konstantinos Drossos -- Tampere University'
__docformat__ = 'reStructuredText'
__all__ = ['training', 'testing', 'experiment']


def _sed_epoch(model, data_loader, objective,
               optimizer, device, is_testing=False,
               use_tf=True, grad_norm=1.):
    """Performs a forward pass for the BREACNNModel model.

    :param model: The BREACNNModel model.
    :type model: torch.nn.Module
    :param data_loader: The data loader to be used.
    :type data_loader: torch.utils.data.DataLoader
    :param objective: The objective function to be used.
    :type objective: callable | None
    :param optimizer: The optimizer ot be used.
    :type optimizer: torch.optim.Optimizer | None
    :param device: The device to be used.
    :type device: str
    :param is_testing: Is it a testing pass?
    :type is_testing: bool
    :param use_tf: Do we use teacher forcing?
    :type use_tf: bool
    :param grad_norm: The maximum gradient norm.
    :type grad_norm: float
    :return: The model and the values for the objective and evaluation of a full\
             iteration of the data (objective, f1_score, er_score).
    :rtype: torch.nn.Module, torch.Tensor, torch.Tensor, torch.Tensor
    """
    epoch_objective_values = zeros(len(data_loader)).float()

    values_true = []
    values_hat = []

    for e, data in enumerate(data_loader):
        if optimizer is not None:
            optimizer.zero_grad()

        x = data[0].float().to(device)
        y = data[1].float().to(device)

        y_hat = model(x, y if not is_testing else None) if use_tf else model(x)

        if objective is not None:
            loss = objective(y_hat, y)
            if optimizer is not None:
                loss.backward()
                if grad_norm > 0:
                    utils.clip_grad_norm_(model.parameters(), grad_norm)
                optimizer.step()
            loss = loss.item()
        else:
            loss = 0.

        epoch_objective_values[e] = loss
        values_true.append(y.cpu())
        values_hat.append(y_hat.cpu())

    values_true = cat(values_true, dim=0)
    values_hat = cat(values_hat, dim=0)

    return model, epoch_objective_values, values_true, values_hat


def testing(model, data_loader, f1_func, er_func, device, use_tf):
    """Tests a model.

    :param model: The model to be tested.
    :type model: torch.nn.Module
    :param data_loader: The data loader to be used.
    :type data_loader: torch.utils.data.DataLoader
    :param f1_func: The function to obtain F1 score.
    :type f1_func: callable
    :param er_func: The function to obtain error rate.
    :type er_func: callable
    :param device: The device to be used.
    :type device: str
    :param use_tf: Do we use teacher forcing?
    :type use_tf: bool
    """
    start_time = time()
    model.eval()
    with no_grad():
        _, _, true_values, hat_values = _sed_epoch(
            model=model, data_loader=data_loader,
            objective=None, optimizer=None,
            device=device, use_tf=use_tf,
            is_testing=True
        )
    end_time = time() - start_time

    f1_score = f1_func(hat_values, true_values).mean()
    er_score = er_func(hat_values, true_values).mean()

    print_evaluation_results(f1_score, er_score, end_time)


def training(model, data_loader_training, optimizer, objective, f1_func, er_func,
             epochs, data_loader_validation, validation_patience, device, grad_norm,
             use_tf=True):
    """Optimizes an BREACNNModel model.

    :param model: The BREACNNModel model.
    :type model: torch.nn.Module
    :param data_loader_training: The data loader to be used with\
                                 the training data.
    :type data_loader_training: torch.utils.data.DataLoader
    :param optimizer: The optimizer ot be used.
    :type optimizer: torch.optim.Optimizer
    :param objective: The objective function to be used.
    :type objective: callable
    :param f1_func: The function to calculate the F1 score.
    :type f1_func: callable
    :param er_func: The function to calculate the error rate.
    :type er_func: callable
    :param epochs: The maximum amount of epochs for training.
    :type epochs: int
    :param data_loader_validation:The data loader to be used with\
                                 the validation data.
    :type data_loader_validation: torch.utils.data.DataLoader
    :param validation_patience: The maximum amount of epochs for waiting\
                                for validation score improvement.
    :type validation_patience: int
    :param device: The device to be used.
    :type device: str
    :param grad_norm: The maximum gradient norm.
    :type grad_norm: float
    :param use_tf: Do we use teacher forcing?
    :type use_tf: bool
    :return: The optimized model.
    :rtype: torch.nn.Module
    """
    best_model = None
    epochs_waiting = 100
    biggest_epoch_loss = 1e8
    best_model_epoch = -1

    for epoch in range(epochs):
        start_time = time()

        model.train()
        model, epoch_tr_loss, true_training, hat_training = _sed_epoch(
            model=model, data_loader=data_loader_training,
            objective=objective, optimizer=optimizer,
            device=device, use_tf=use_tf, grad_norm=grad_norm
        )

        epoch_tr_loss = epoch_tr_loss.mean()

        f1_score_training = f1_func(hat_training, true_training).mean()
        error_rate_training = er_func(hat_training, true_training).mean()

        model.eval()
        with no_grad():
            model, epoch_va_loss, true_validation, hat_validation = _sed_epoch(
                model=model, data_loader=data_loader_validation,
                objective=objective, optimizer=None,
                device=device, use_tf=use_tf, is_testing=True
            )

        epoch_va_loss = epoch_va_loss.mean()

        f1_score_validation = f1_func(hat_validation, true_validation).mean()
        error_rate_validation = er_func(hat_validation, true_validation).mean()

        if epoch_va_loss < biggest_epoch_loss:
            biggest_epoch_loss = epoch_va_loss
            epochs_waiting = 0
            best_model = deepcopy(model.state_dict())
            best_model_epoch = epoch
        else:
            epochs_waiting += 1

        end_time = time() - start_time

        print_training_results(
            epoch=epoch, training_loss=epoch_tr_loss,
            validation_loss=epoch_va_loss,
            training_f1=f1_score_training,
            training_er=error_rate_training,
            validation_f1=f1_score_validation,
            validation_er=error_rate_validation,
            time_elapsed=end_time
        )

        if epochs_waiting >= validation_patience:
            print_msg(
                'Early stopping! Lowest validation loss: {:7.3f} at epoch: {:3d}'.format(
                    biggest_epoch_loss, best_model_epoch
                ), start='\n-- ', end='\n\n')
            break

    if best_model is not None:
        model.load_state_dict(best_model)

    return model


def experiment(settings, model_class, use_tf):
    """Does the experiment with the specified settings and model.

    :param settings: The settings.
    :type settings: dict
    :param model_class: The class of the model.
    :type model_class: callable
    :param use_tf: Do we use teacher forcing?
    :type use_tf: bool
    """
    device = 'cuda' if is_available() else 'cpu'
    inform_about_device(device)

    with InformAboutProcess('Creating the model'):
        model_settings = settings['sed_model']
        if use_tf:
            model_settings.update(settings['tf'])
        model = model_class(**model_settings)
        model = model.to(device)

    with InformAboutProcess('Creating training data loader'):
        training_data = get_tut_sed_data_loader(
            split='training', **settings['data_loader'],
            is_test=False
        )

    with InformAboutProcess('Creating validation data loader'):
        validation_data = get_tut_sed_data_loader(
            split='validation', **settings['data_loader'],
            is_test=True
        )

    with InformAboutProcess('Creating optimizer'):
        optimizer = Adam(model.parameters(), lr=settings['optimizer']['lr'])

    if use_tf:
        with InformAboutProcess('Setting the teacher forcing batch counter'):
            model.batch_counter = len(training_data)

    print_msg('', start='')

    common_kwargs = {
        'f1_func': f1_per_frame,
        'er_func': error_rate_per_frame,
        'device': device,
        'use_tf': use_tf
    }

    len_m = max([
        len('Training examples/batches'),
        len('Validation examples/batches'),
        len('Testing examples/batches')
    ])

    print_msg('{m:<{len_m}}: {d1:5d} /{d2:5d}'.format(
        m='Training examples/batches',
        d1=len(training_data) * settings['data_loader']['batch_size'],
        d2=len(training_data),
        len_m=len_m
    ))

    print_msg('{m:<{len_m}}: {d1:5d} /{d2:5d}'.format(
        m='Validation examples/batches',
        d1=len(validation_data) * settings['data_loader']['batch_size'],
        d2=len(validation_data),
        len_m=len_m
    ), end='\n\n')

    print_msg('Starting training', start='\n\n-- ', end='\n\n')

    optimized_model = training(
        model=model, data_loader_training=training_data,
        optimizer=optimizer, objective=BCEWithLogitsLoss(),
        epochs=settings['training']['epochs'],
        data_loader_validation=validation_data,
        validation_patience=settings['training']['validation_patience'],
        grad_norm=settings['training']['grad_norm'], **common_kwargs
    )

    del training_data

    if settings['data_loader']['data_version'] == 'synthetic':
        del validation_data
        print_msg('Using separate testing split.', start='\n\n-- ')
        with InformAboutProcess('Creating testing data loader'):
            testing_data = get_tut_sed_data_loader(
                split='testing', **settings['data_loader'])

        print_msg('{m:<{len_m}}: {d1:5d} /{d2:5d}'.format(
            m='Testing examples/batches',
            d1=len(testing_data) * settings['data_loader']['batch_size'],
            d2=len(testing_data),
            len_m=len_m
        ))
    else:
        print_msg('Using X-fold setting.', start='\n\n-- ')
        testing_data = validation_data

    print_msg('Starting testing', start='\n\n-- ', end='\n\n')
    testing(
        model=optimized_model, data_loader=testing_data,
        **common_kwargs
    )

    print_msg('That\'s all!', start='\n\n-- ', end='\n\n')

# EOF

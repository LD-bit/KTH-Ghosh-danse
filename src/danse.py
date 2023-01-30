import numpy as np
import torch
from torch.autograd import Variable
from torch import nn, optim, distributions
from timeit import default_timer as timer
import sys
import copy
import math
import os
from utils.utils import compute_log_prob_normal, create_diag, compute_inverse, count_params, ConvergenceMonitor
from utils.plot_functions import plot_state_trajectory, plot_state_trajectory_axes
import torch.nn.functional as F

# Create an RNN model for prediction
class RNN_model(nn.Module):
    """ This super class defines the specific model to be used i.e. LSTM or GRU or RNN
    """
    def __init__(self, input_size, output_size, n_hidden, n_layers, 
        model_type, lr, num_epochs, n_hidden_dense=32, num_directions=1, batch_first = True, min_delta=1e-2):
        super(RNN_model, self).__init__()
        """
        Args:
        - input_size: The dimensionality of the input data
        - output_size: The dimensionality of the output data
        - n_hidden: The size of the hidden layer, i.e. the number of hidden units used
        - n_layers: The number of hidden layers
        - model_type: The type of modle used ("lstm"/"gru"/"rnn")
        - lr: Learning rate used for training
        - num_epochs: The number of epochs used for training
        - num_directions: Parameter for bi-directional RNNs (usually set to 1 in this case, for bidirectional set as 2)
        - batch_first: Option to have batches with the batch dimension as the starting dimension 
        of the input data
        """
        # Defining some parameters
        self.hidden_dim = n_hidden  
        self.num_layers = n_layers
        self.input_size = input_size
        self.output_size = output_size
        
        self.model_type = model_type
        self.lr = lr
        self.num_epochs = num_epochs
        
        # Predefined:
        ## Use only the forward direction 
        self.num_directions = num_directions
        
        ## The input tensors must have shape (batch_size,...)
        self.batch_first = batch_first
        
        # Defining the recurrent layers 
        if model_type.lower() == "rnn": # RNN 
            self.rnn = nn.RNN(input_size=self.input_size, hidden_size=self.hidden_dim, 
                num_layers=self.num_layers, batch_first=self.batch_first)   
        elif model_type.lower() == "lstm": # LSTM
            self.rnn = nn.LSTM(input_size=self.input_size, hidden_size=self.hidden_dim, 
                num_layers=self.num_layers, batch_first=self.batch_first)
        elif model_type.lower() == "gru": # GRU
            self.rnn = nn.GRU(input_size=self.input_size, hidden_size=self.hidden_dim, 
                num_layers=self.num_layers, batch_first=self.batch_first)  
        else:
            print("Model type unknown:", model_type.lower()) 
            sys.exit() 
        
        # Fully connected layer to be used for mapping the output
        #self.fc = nn.Linear(self.hidden_dim * self.num_directions, self.output_size)
        
        self.fc = nn.Linear(self.hidden_dim * self.num_directions, n_hidden_dense)
        self.fc_mean = nn.Linear(n_hidden_dense, self.output_size)
        self.fc_vars = nn.Linear(n_hidden_dense, self.output_size)
        # Add a dropout layer with 20% probability
        #self.d1 = nn.Dropout(p=0.2)

    def init_h0(self, batch_size):
        """ This function defines the initial hidden state of the RNN
        """
        # This method generates the first hidden state of zeros (h0) which is used in the forward pass
        h0 = torch.randn(self.num_layers, batch_size, self.hidden_dim)
        return h0
    
    def forward(self, x):
        """ This function defines the forward function to be used for the RNN model
        """
        batch_size = x.shape[0]
        
        # Obtain the RNN output
        r_out, _ = self.rnn(x)
        
        # Reshaping the output appropriately
        r_out_all_steps = r_out.contiguous().view(batch_size, -1, self.num_directions * self.hidden_dim)

        # Passing the output to one fully connected layer
        y = F.relu(self.fc(r_out_all_steps))

        # Means and variances are computed for time instants t=2, ..., T+1 using the available sequence
        mu_2T_1 = self.fc_mean(y) # A second linear projection to get the means
        vars_2T_1 = F.softplus(self.fc_vars(y)) # A second linear projection followed by an activation function to get variances

        # The mean and variances at the first time step need to be computed only based on the previous hidden state
        mu_1 = self.fc_mean(F.relu(self.fc(self.init_h0(batch_size)[-1,:,:]))).view(batch_size, 1, -1)
        var_1 = F.softplus(self.fc_vars(F.relu(self.fc(self.init_h0(batch_size)[-1,:,:]))).view(batch_size, 1, -1))

        # To get the means and variances for the time instants t=1, ..., T, we take the previous result and concatenate 
        # all but last value to the value found at t=1. Concatenation is done along the sequence dimension
        mu = torch.cat(
            (mu_1, mu_2T_1[:,:-1,:]),
            dim=1
        )

        vars = torch.cat(
            (var_1, vars_2T_1[:,:-1,:]),
            dim=1
        )

        return mu, vars

def save_model(model, filepath):
    torch.save(model.state_dict(), filepath)
    return None

def push_model(nets, device='cpu'):
    nets = nets.to(device=device)
    return nets

class DANSE(nn.Module):

    def __init__(self, n_states, n_obs, mu_w, C_w, H, mu_x0, C_x0, batch_size, rnn_type, rnn_params_dict, device='cpu'):
        super(DANSE, self).__init__()

        self.device = device

        # Initialize the paramters of the state estimator
        self.n_states = n_states
        self.n_obs = n_obs
        
        # Initializing the parameters of the initial state
        self.mu_x0 = self.push_to_device(mu_x0)
        self.C_x0 = self.push_to_device(C_x0)

        # Initializing the parameters of the measurement noise
        self.mu_w = self.push_to_device(mu_w)
        self.C_w = self.push_to_device(C_w)

        # Initialize the observation model matrix 
        self.H = self.push_to_device(H)
        
        self.batch_size = batch_size

        # Initialize RNN type
        self.rnn_type = rnn_type

        # Initialize the parameters of the RNN
        self.rnn = RNN_model(**rnn_params_dict[self.rnn_type])

        # Initialize various means and variances of the estimator

        # Prior parameters
        self.mu_xt_yt_current = None
        self.L_xt_yt_current = None

        # Marginal parameters
        self.mu_yt_current = None
        self.L_yt_current = None

        # Posterior parameters
        self.mu_xt_yt_prev = None
        self.L_xt_yt_prev = None
    
    def push_to_device(self, x):
        """ Push the given tensor to the device
        """
        return torch.from_numpy(x).type(torch.FloatTensor).to(self.device)

    def compute_prior_mean_vars(self, mu_xt_yt_prev, L_xt_yt_prev):

        self.mu_xt_yt_prev = mu_xt_yt_prev
        self.L_xt_yt_prev = create_diag(L_xt_yt_prev)
        return self.mu_xt_yt_prev, self.L_xt_yt_prev

    def compute_marginal_mean_vars(self, mu_xt_yt_prev, L_xt_yt_prev):

        self.mu_yt_current = torch.einsum('ij,ntj->nti',self.H, mu_xt_yt_prev) + self.mu_w.squeeze(-1)
        self.L_yt_current = self.H @ L_xt_yt_prev @ self.H.T + self.C_w
    
    def compute_posterior_mean_vars(self, Yi_batch):

        Re_t_inv = torch.inverse(self.H @ self.L_xt_yt_prev @ self.H.T + self.C_w)
        self.K_t = (self.L_xt_yt_prev @ (self.H.T @ Re_t_inv))
        self.mu_xt_yt_current = self.mu_xt_yt_prev + torch.einsum('ntij,ntj->nti',self.K_t,(Yi_batch - torch.einsum('ij,ntj->nti',self.H,self.mu_xt_yt_prev)))
        self.L_xt_yt_current = self.L_xt_yt_prev - self.K_t @ (self.H @ self.L_xt_yt_prev @ self.H.T + self.C_w) @ self.K_t.T
        self.L_xt_yt_current = self.L_xt_yt_prev - torch.einsum('ntij,ntkl->ntik',
        torch.einsum('ntij,ntjk->ntik',
        self.K_t, self.H @ self.L_xt_yt_prev @ self.H.T + self.C_w), 
        self.K_t)

        return self.mu_xt_yt_current, self.L_xt_yt_current
    '''
    def compute_logprob_batch(self, Yi_batch):

        N, T, _ = Yi_batch.shape
        log_py_t_given_prev = 0.0 
        for t in range(T):

            if t >= 1:
                mu_yt_prev, L_yt_prev_diag = self.rnn(Yi_batch[:, 0:t-1, :])
            else:
                mu_yt_prev, L_yt_prev_diag = self.rnn(torch.zeros(N, 1, 1))
            
            self.compute_prior_mean_vars(mu_yt_prev, L_yt_prev_diag)
            self.compute_marginal_mean_vars()

            log_py_t_given_prev += compute_log_prob_normal(
                X = Yi_batch[:, t, :],
                mean=self.mu_yt_current,
                cov=self.L_yt_current
                ).mean(0)

        return log_py_t_given_prev
    '''
    def compute_logpdf_Gaussian(self, Y):
        
        _, T, _ = Y.shape 
        logprob = 0.5 * self.n_obs * T * math.log(math.pi*2) - 0.5 * torch.logdet(self.L_yt_current).sum(1) \
            - 0.5 * torch.einsum('nti,nti->nt',
            (Y - self.mu_yt_current), 
            torch.einsum('ntij,ntj->nti',torch.inverse(self.L_yt_current), (Y - self.mu_yt_current))).sum(1)

        return logprob

    def compute_predictions(self, Y_test_batch):

        mu_x_given_Y_test_batch, vars_x_given_Y_test_batch = self.rnn.forward(x=Y_test_batch)
        mu_xt_yt_prev_test, L_xt_yt_prev_test = self.compute_prior_mean_vars(
            mu_xt_yt_prev=mu_x_given_Y_test_batch,
            L_xt_yt_prev=vars_x_given_Y_test_batch
            )
        mu_xt_yt_current_test, L_xt_yt_current_test = self.compute_posterior_mean_vars(Yi_batch=Y_test_batch)
        return mu_xt_yt_prev_test, L_xt_yt_prev_test, mu_xt_yt_current_test, L_xt_yt_current_test

    def forward(self, Yi_batch):

        mu_batch, vars_batch = self.rnn.forward(x=Yi_batch)
        mu_xt_yt_prev, L_xt_yt_prev = self.compute_prior_mean_vars(mu_xt_yt_prev=mu_batch, L_xt_yt_prev=vars_batch)
        self.compute_marginal_mean_vars(mu_xt_yt_prev=mu_xt_yt_prev, L_xt_yt_prev=L_xt_yt_prev)
        logprob_batch = self.compute_logpdf_Gaussian(Y=Yi_batch) / (Yi_batch.shape[1] * Yi_batch.shape[2]) # Per dim. and per sequence length
        log_pYT_batch_avg = logprob_batch.mean(0)

        return log_pYT_batch_avg


def train_danse(model, options, train_loader, val_loader, nepochs, logfile_path, modelfile_path, save_chkpoints, device='cpu', tr_verbose=False):
    
    # Push the model to device and count parameters
    model = push_model(nets=model, device=device)
    total_num_params, total_num_trainable_params = count_params(model)
    
    # Set the model to training
    model.train()
    optimizer = optim.Adam(model.parameters(), lr=model.rnn.lr)
    #scheduler = optim.lr_scheduler.ExponentialLR(optimizer, gamma=0.998)
    #scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=nepochs//3, gamma=0.9) # gamma was initially 0.9
    scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=nepochs//6, gamma=0.9) # gamma is now set to 0.8
    tr_losses = []
    val_losses = []

    if modelfile_path is None:
        model_filepath = "./models/"
    else:
        model_filepath = modelfile_path

    #if save_chkpoints == True:
    if save_chkpoints == "all" or save_chkpoints == "some":
        # No grid search
        if logfile_path is None:
            training_logfile = "./log/danse_{}.log".format(model.rnn_type)
        else:
            training_logfile = logfile_path

    elif save_chkpoints == None:
        # Grid search
        if logfile_path is None:
            training_logfile = "./log/gs_training_danse_{}.log".format(model.rnn_type)
        else:
            training_logfile = logfile_path
    
    # Call back parameters
    
    patience = 0
    num_patience = 3 
    min_delta = options['rnn_params_dict'][model.rnn_type]["min_delta"] # 1e-3 for simpler model, for complicated model we use 1e-2
    #min_tol = 1e-3 # for tougher model, we use 1e-2, easier models we use 1e-5
    check_patience=False
    best_val_loss = np.inf
    tr_loss_for_best_val_loss = np.inf
    best_model_wts = None
    best_val_epoch = None
    orig_stdout = sys.stdout
    f_tmp = open(training_logfile, 'a')
    sys.stdout = f_tmp
    

    # Convergence monitoring (checks the convergence but not ES of the val_loss)
    model_monitor = ConvergenceMonitor(tol=min_delta,
                                    max_epochs=num_patience)

    # This checkes the ES of the val loss, if the loss deteriorates for specified no. of
    # max_epochs, stop the training
    #model_monitor = ConvergenceMonitor_ES(tol=min_tol, max_epochs=num_patience)

    print("------------------------------ Training begins --------------------------------- \n")
    print("Config: {} \n".format(options))
    print("\n Config: {} \n".format(options), file=orig_stdout)
    print("No. of trainable parameters: {}\n".format(total_num_trainable_params), file=orig_stdout)
    print("No. of trainable parameters: {}\n".format(total_num_trainable_params))

    # Start time
    starttime = timer()
    try:
        for epoch in range(nepochs):
            
            tr_running_loss = 0.0
            tr_loss_epoch_sum = 0.0
            val_loss_epoch_sum = 0.0
        
            for i, data in enumerate(train_loader, 0):
            
                tr_Y_batch, tr_X_batch = data
                optimizer.zero_grad()
                Y_train_batch = Variable(tr_Y_batch, requires_grad=False).type(torch.FloatTensor).to(device)
                log_pY_train_batch = -model.forward(Y_train_batch)
                log_pY_train_batch.backward()
                optimizer.step()

                # print statistics
                tr_running_loss += log_pY_train_batch.item()
                tr_loss_epoch_sum += log_pY_train_batch.item()

                if i % 100 == 99 and ((epoch + 1) % 100 == 0):    # print every 10 mini-batches
                    #print("Epoch: {}/{}, Batch index: {}, Training loss: {}".format(epoch+1, nepochs, i+1, tr_running_loss / 100))
                    #print("Epoch: {}/{}, Batch index: {}, Training loss: {}".format(epoch+1, nepochs, i+1, tr_running_loss / 100), file=orig_stdout)
                    tr_running_loss = 0.0
            
            #scheduler.step()

            endtime = timer()
            # Measure wallclock time
            time_elapsed = endtime - starttime

            with torch.no_grad():
                
                for i, data in enumerate(val_loader, 0):
                    
                    val_Y_batch, val_X_batch = data
                    Y_val_batch = Variable(val_Y_batch, requires_grad=False).type(torch.FloatTensor).to(device)
                    log_pY_val_batch = -model.forward(Y_val_batch)
                    val_loss_epoch_sum += log_pY_val_batch.item()
            
            # Loss at the end of each epoch
            tr_loss = tr_loss_epoch_sum / len(train_loader)
            val_loss = val_loss_epoch_sum / len(val_loader)

            # Record the validation loss per epoch
            #if (epoch + 1) > 100: # nepochs/6 for complicated, 100 for simpler model
            #    model_monitor.record(val_loss)

            # Displaying loss at an interval of 200 epochs
            if tr_verbose == True and (((epoch + 1) % 5) == 0 or epoch == 0):
                
                print("Epoch: {}/{}, Training NLL:{:.9f}, Val. NLL:{:.9f} ".format(epoch+1, 
                model.rnn.num_epochs, tr_loss, val_loss), file=orig_stdout)
                #save_model(model, model_filepath + "/" + "{}_ckpt_epoch_{}.pt".format(model.model_type, epoch+1))

                print("Epoch: {}/{}, Training NLL:{:.9f}, Val. NLL:{:.9f}, Time_Elapsed:{:.4f} secs".format(epoch+1, 
                model.rnn.num_epochs, tr_loss, val_loss, time_elapsed))
            
            # Checkpointing the model every few  epochs
            #if (((epoch + 1) % 500) == 0 or epoch == 0) and save_chkpoints == True:     
            if (((epoch + 1) % 100) == 0 or epoch == 0) and save_chkpoints == "all": 
                # Checkpointing model every few epochs, in case of grid_search is being done, save_chkpoints = None
                save_model(model, model_filepath + "/" + "danse_{}_ckpt_epoch_{}.pt".format(model.rnn_type, epoch+1))
            elif (((epoch + 1) % nepochs) == 0) and save_chkpoints == "some": 
                # Checkpointing model at the end of training epochs, in case of grid_search is being done, save_chkpoints = None
                save_model(model, model_filepath + "/" + "danse_{}_ckpt_epoch_{}.pt".format(model.rnn_type, epoch+1))
            
            # Save best model in case validation loss improves
            '''
            best_val_loss, best_model_wts, best_val_epoch, patience, check_patience = callback_val_loss(model=model,
                                                                                                    best_model_wts=best_model_wts,
                                                                                                    val_loss=val_loss,
                                                                                                    best_val_loss=best_val_loss,
                                                                                                    best_val_epoch=best_val_epoch,
                                                                                                    current_epoch=epoch+1,
                                                                                                    patience=patience,
                                                                                                    num_patience=num_patience,
                                                                                                    min_delta=min_delta,
                                                                                                    check_patience=check_patience,
                                                                                                    orig_stdout=orig_stdout)
            if check_patience == True:
                print("Monitoring validation loss for criterion", file=orig_stdout)
                print("Monitoring validation loss for criterion")
            else:
                pass
            
            if val_loss < best_val_loss:
                best_val_loss = val_loss # Save best validation loss
                tr_loss_for_best_val_loss = tr_loss # Training loss corresponding to best validation loss
                best_val_epoch = epoch+1 # Corresponding value of epoch
                best_model_wts = copy.deepcopy(model.state_dict()) # Weights for the best model
            '''
            # Saving every value
            tr_losses.append(tr_loss)
            val_losses.append(val_loss)

            '''
            # Check monitor flag
            if model_monitor.monitor(epoch=epoch+1) == True:

                if tr_verbose == True:
                    print("Training convergence attained! Saving model at Epoch: {}".format(epoch+1), file=orig_stdout)
                
                print("Training convergence attained at Epoch: {}!".format(epoch+1))
                # Save the best model as per validation loss at the end
                best_val_loss = val_loss # Save best validation loss
                tr_loss_for_best_val_loss = tr_loss # Training loss corresponding to best validation loss
                best_val_epoch = epoch+1 # Corresponding value of epoch
                best_model_wts = copy.deepcopy(model.state_dict()) # Weights for the best model
                #print("\nSaving the best model at epoch={}, with training loss={}, validation loss={}".format(best_val_epoch, tr_loss_for_best_val_loss, best_val_loss))
                #save_model(model, model_filepath + "/" + "{}_usenorm_{}_ckpt_epoch_{}.pt".format(model.model_type, usenorm_flag, epoch+1))
                break

            #else:

                #print("Model improvement attained at Epoch: {}".format(epoch+1))
                #best_val_loss = val_loss # Save best validation loss
                #tr_loss_for_best_val_loss = tr_loss # Training loss corresponding to best validation loss
                #best_val_epoch = epoch+1 # Corresponding value of epoch
                #best_model_wts = copy.deepcopy(model.state_dict()) # Weights for the best model

            '''
        # Save the best model as per validation loss at the end
        print("\nSaving the best model at epoch={}, with training loss={}, validation loss={}".format(best_val_epoch, tr_loss_for_best_val_loss, best_val_loss))
        
        #if save_chkpoints == True:
        if save_chkpoints == "all" or save_chkpoints == "some":
            # Save the best model using the designated filename
            if not best_model_wts is None:
                model_filename = "danse_{}_ckpt_epoch_{}_best.pt".format(model.rnn_type, best_val_epoch)
                torch.save(best_model_wts, model_filepath + "/" + model_filename)
            else:
                model_filename = "danse_{}_ckpt_epoch_{}_best.pt".format(model.rnn_type, epoch+1)
                print("Saving last model as best...")
                save_model(model, model_filepath + "/" + model_filename)
        #elif save_chkpoints == False:
        elif save_chkpoints == None:
            pass
    
    except KeyboardInterrupt:

        if tr_verbose == True:
            print("Interrupted!! ...saving the model at epoch:{}".format(epoch+1), file=orig_stdout)
            print("Interrupted!! ...saving the model at epoch:{}".format(epoch+1))
        else:
            print("Interrupted!! ...saving the model at epoch:{}".format(epoch+1))

        model_filename = "{}_usenorm_{}_ckpt_epoch_{}_latest.pt".format(model.rnn_type, epoch+1)
        torch.save(model, model_filepath + "/" + model_filename)

    print("------------------------------ Training ends --------------------------------- \n")
    # Restoring the original std out pointer
    sys.stdout = orig_stdout

    return tr_losses, val_losses, best_val_loss, tr_loss_for_best_val_loss, model

def test_danse(test_loader, options, device, model_file=None, test_logfile_path = None):

    test_loss_epoch_sum = 0.0
    te_log_pY_epoch_sum = 0.0 
    print("################ Evaluation Begins ################ \n")    
    
    # Set model in evaluation mode
    model = DANSE(**options)
    model.load_state_dict(torch.load(model_file))
    criterion = nn.MSELoss()
    model = push_model(nets=model, device=device)
    model.eval()
    if not test_logfile_path is None:
        test_log = "./log/test_danse.log"
    else:
        test_log = test_logfile_path

    X_ref = None
    X_hat_ref = None

    with torch.no_grad():
        
        for i, data in enumerate(test_loader, 0):
                
            te_Y_batch, te_X_batch = data
            Y_test_batch = Variable(te_Y_batch, requires_grad=False).type(torch.FloatTensor).to(device)
            te_mu_X_predictions_batch, te_var_X_predictions_batch, te_mu_X_filtered_batch, te_var_X_filtered_batch = model.compute_predictions(Y_test_batch)
            log_pY_test_batch = -model.forward(Y_test_batch)
            test_mse_loss_batch = criterion(te_X_batch, te_mu_X_filtered_batch)
            # print statistics
            test_loss_epoch_sum += test_mse_loss_batch.item()
            te_log_pY_epoch_sum += log_pY_test_batch.item()

        X_ref = te_X_batch[-1]
        X_hat_ref = te_mu_X_filtered_batch[-1]

    test_mse_loss = test_loss_epoch_sum / len(test_loader)
    test_NLL_loss = te_log_pY_epoch_sum / len(test_loader)

    print('Test NLL loss: {:.3f}, Test MSE loss: {:.3f} using weights from file: {} %'.format(test_NLL_loss, test_mse_loss, model_file))

    with open(test_log, "a") as logfile_test:
        logfile_test.write('Test NLL loss: {:.3f}, Test MSE loss: {:.3f} using weights from file: {}'.format(test_NLL_loss, test_mse_loss, model_file))

    # Plot one of the predictions
    plot_state_trajectory(X=X_ref, X_est=X_hat_ref)
    plot_state_trajectory_axes(X=X_ref, X_est=X_hat_ref)

    return test_mse_loss   


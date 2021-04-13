from dataset import PtbEcgDataset
import click
from torch.nn.functional import binary_cross_entropy
from torch import square
from torch import sum
import torch.optim as optim
from gnn_models import *
import numpy as np
from sklearn.metrics import roc_auc_score
from sklearn.metrics import f1_score
from sklearn.model_selection import train_test_split
from torch.utils.data.sampler import SubsetRandomSampler
from torch.utils.data import DataLoader
from visdom import Visdom
import torch
from sklearn.metrics import confusion_matrix
import networkx as nx
import matplotlib.pyplot as plt
from networkx import normalized_laplacian_matrix


def generation_classification_loss(generated_graph,
                                   original_graph,
                                   classification_predictions,
                                   classification_labels,
                                   h_log_std,
                                   h_mean):
    """Computes the overall loss

    Combines the generator and classification loss to find the overall cost. This cost contains the ELBO loss and the
    classification loss.

    Args:
        generated_graph:
            The graph generated by the generator
        original_graph:
            The original graph fed into the generator
        classification_predictions:
            The classifier's prediction
        classification_labels:
            The labels for the classifier
        h_log_std:
            The log std computed by the generator
        h_mean:
            The mean computed by the generator
    """
    # Compute the reconstruction loss
    reconstruction_loss = binary_cross_entropy(generated_graph, original_graph)

    # Compute the KL loss
    kl_loss = torch.mean(sum(1 + 2*h_log_std - square(h_mean) - square(exp(h_log_std)), dim=1))

    # Compute the classification loss
    classification_loss = binary_cross_entropy(classification_predictions, classification_labels)

    # if classification_labels[0] == 0:
    #     classification_loss = 4*classification_loss

    # Add the classification loss
    cost = 10*classification_loss + reconstruction_loss - kl_loss

    return cost, reconstruction_loss, -kl_loss, classification_loss


@click.command()
@click.argument('train_data_dir', type=click.Path(exists=True))
@click.argument('train_label_dir', type=click.Path(exists=True))
@click.option('--history_path', '-hp', default='../../reports/training_history/tr')
# @click.option('--history_path', '-hp', default=None)
def train(train_data_dir, train_label_dir, history_path):

    torch.manual_seed(10)

    # Load the dataset
    dataset = PtbEcgDataset(input_data_csv_file=train_data_dir, input_label_csv_file=train_label_dir)

    # Make validation and training split in a stratified fashion
    train_idx, val_idx = train_test_split(np.arange(len(dataset)),
                                          test_size=0.2,
                                          random_state=60,
                                          stratify=dataset.label,
                                          shuffle=True)

    train_dataset = DataLoader(dataset, sampler=SubsetRandomSampler(train_idx))
    val_dataset = DataLoader(dataset, sampler=SubsetRandomSampler(val_idx))

    print('Dataset has {} control samples'.format(dataset.num_healthy_samps))
    print('Dataset has {} unhealthy samples'.format(dataset.num_unhealthy_samps))

    print('Training dataset has {} samples'.format(len(train_dataset)))
    print('Validation dataset has {} samples'.format(len(val_dataset)))

    labels = train_dataset.dataset.label[train_dataset.sampler.indices]
    print('Training dataset has {} control samples'.format(np.count_nonzero(labels == 0)))
    print('Training dataset has {} unhealthy samples'.format(np.count_nonzero(labels != 0)))

    labels = val_dataset.dataset.label[val_dataset.sampler.indices]
    print('Validation dataset has {} control samples'.format(np.count_nonzero(labels == 0)))
    print('Validation dataset has {} unhealthy samples'.format(np.count_nonzero(labels != 0)))

    # Define classification and generation models
    generator_model = VariationalGraphAutoEncoder(input_dim=10,
                                                  hidden_dim_1=8,
                                                  hidden_dim_2=6,
                                                  num_nodes=15)
    classifier_model = BinaryGraphClassifier(input_dim=10, hidden_dim_1=8, hidden_dim_2=6)

    # Optimizers for the classification and generator process
    # graph_generator_optimizer = optim.Adam(generator_model.parameters(), lr=1e-4, weight_decay=1e-3)
    # graph_classifier_optimizer = optim.Adam(classifier_model.parameters(), lr=1e-4, weight_decay=1e-3)
    graph_generator_optimizer = optim.Adam(generator_model.parameters(), lr=1e-4)
    graph_classifier_optimizer = optim.Adam(classifier_model.parameters(), lr=1e-4)

    # Scheduler
    # scheduler_gen = torch.optim.lr_scheduler.MultiStepLR(graph_classifier_optimizer, milestones=[100, 150, 200], gamma=0.5)
    # scheduler_cl = torch.optim.lr_scheduler.MultiStepLR(graph_classifier_optimizer, milestones=[100, 150, 200], gamma=0.5)

    # Initialize visualizer
    vis = Visdom()

    # Holds the maximum validation accuracy
    max_validation_acc = 0

    # Colour map for networkx
    color_map = range(15)

    # classification threshold
    threshold = 0.55

    # Create the input graph to the GVAE
    nx_graph = nx.from_numpy_matrix(np.ones((15, 15)))
    normalized_graph = normalized_laplacian_matrix(nx_graph).toarray()
    normalized_graph = torch.tensor(normalized_graph)
    normalized_graph.requires_grad = False
    normalized_graph = normalized_graph.type(torch.FloatTensor)

    for epoch in range(10000):

        # Put models in training mode
        generator_model.train()
        classifier_model.train()

        y_true = list()
        y_pred = list()
        epoch_loss = 0
        epoch_recl = 0
        epoch_kl = 0
        epoch_cl = 0
        num_edges = 0

        for features, label in train_dataset:

            generated_graph = generator_model(torch.ones((15, 15)), features[0], False)

            classification_predictions = classifier_model(generated_graph, features[0])

            y_true.append(label.numpy().flatten())
            y_pred.append(classification_predictions.detach().numpy().flatten())

            # ELBO Loss
            loss, recl, kl, cl = generation_classification_loss(generated_graph=generated_graph,
                                                                original_graph=torch.ones((15, 15)),
                                                                classification_predictions=classification_predictions,
                                                                classification_labels=label[0],
                                                                h_log_std=generator_model.h_log_std,
                                                                h_mean=generator_model.h_mean)

            graph_generator_optimizer.zero_grad()
            graph_classifier_optimizer.zero_grad()

            loss.backward()

            graph_generator_optimizer.step()
            graph_classifier_optimizer.step()

            epoch_loss += loss.detach().item()
            epoch_recl += recl.detach().item()
            epoch_kl += kl.detach().item()
            epoch_cl += cl.detach().item()

            # Count the number of edges
            adj = torch.where(generated_graph.detach() > 0.5, 1, 0)
            num_edges += (torch.count_nonzero(adj).detach().item()-15)/2

        epoch_loss /= len(train_dataset)
        epoch_recl /= len(train_dataset)
        epoch_kl /= len(train_dataset)
        epoch_cl /= len(train_dataset)
        num_edges /= len(train_dataset)
        y_true = np.array(y_true).flatten()
        y_pred = np.array(y_pred).flatten()
        pred_classes = np.where(y_pred.reshape((-1,)) > threshold, 1, 0)

        print('Training epoch {}, loss {:.4f}'.format(epoch, epoch_loss))

        # Compute the roc_auc accuracy
        acc = f1_score(y_true.reshape((-1,)), pred_classes.reshape(-1,))
        print("Training epoch {}, accuracy {:.4f}".format(epoch, acc))

        # Print average number of edges
        print("Training epoch {}, num edges {:.4f}".format(epoch, num_edges))

        vis.line(Y=torch.reshape(torch.tensor(epoch_loss), (-1, )), X=torch.reshape(torch.tensor(epoch), (-1, )),
                 update='append', win='train_loss', name='train_trace',
                 opts=dict(title="Losses Per Epoch", xlabel="Epoch", ylabel="Loss"))

        vis.line(Y=torch.reshape(torch.tensor(acc), (-1, )), X=torch.reshape(torch.tensor(epoch), (-1, )),
                 update='append', win='train_acc', name='train_trace',
                 opts=dict(title="Accuracy Per Epoch", xlabel="Epoch", ylabel="Accuracy"))

        vis.line(Y=torch.reshape(torch.tensor(epoch_recl), (-1, )), X=torch.reshape(torch.tensor(epoch), (-1, )),
                 update='append', win='train_recl', name='train_trace',
                 opts=dict(title="Reconstruction Losses Per Epoch", xlabel="Epoch", ylabel="Loss"))

        vis.line(Y=torch.reshape(torch.tensor(epoch_kl), (-1, )), X=torch.reshape(torch.tensor(epoch), (-1, )),
                 update='append', win='train_kl', name='train_trace',
                 opts=dict(title="KL Losses Per Epoch", xlabel="Epoch", ylabel="Loss"))

        vis.line(Y=torch.reshape(torch.tensor(epoch_cl), (-1, )), X=torch.reshape(torch.tensor(epoch), (-1, )),
                 update='append', win='train_cl', name='train_trace',
                 opts=dict(title="Classification Losses Per Epoch", xlabel="Epoch", ylabel="Loss"))

        vis.line(Y=torch.reshape(torch.tensor(num_edges), (-1, )), X=torch.reshape(torch.tensor(epoch), (-1, )),
                 update='append', win='train_num_edge', name='train_trace',
                 opts=dict(title="Average number of graph edges", xlabel="Epoch", ylabel="Number of Edges"))


        if history_path:
            f = open(history_path+"_train_losses.txt", "a")
            f.write(str(epoch_loss) + "\n")
            f.close()

            f = open(history_path + "_train_accs.txt", "a")
            f.write(str(acc) + "\n")
            f.close()

            # Draw and save the graph every 500 iterations
            if epoch % 3 == 0:
                adj = torch.where(generated_graph.detach() > 0.5, 1, 0)
                nx_graph = nx.from_numpy_matrix(adj.numpy())
                nx.draw(nx_graph, node_color=color_map, with_labels=True)
                plt.savefig(history_path+"_train_graph_"+str(epoch)+".png", format="PNG")
                plt.close()

        with torch.no_grad():
            y_true = list()
            y_pred = list()
            epoch_loss = 0
            epoch_recl = 0
            epoch_kl = 0
            epoch_cl = 0
            num_edges = 0

            generator_model.eval()
            classifier_model.eval()

            for features, label in val_dataset:

                generated_graph = generator_model(torch.ones((15, 15)), features[0], True)

                classification_predictions = classifier_model(generated_graph, features[0])

                y_true.append(label.numpy().flatten())
                y_pred.append(classification_predictions.detach().numpy().flatten())

                # ELBO Loss
                loss, recl, kl, cl = generation_classification_loss(generated_graph=generated_graph,
                                                                    original_graph=torch.ones((15, 15)),
                                                                    classification_predictions=classification_predictions,
                                                                    classification_labels=label[0],
                                                                    h_log_std=generator_model.h_log_std,
                                                                    h_mean=generator_model.h_mean)

                epoch_loss += loss.detach().item()
                epoch_recl += recl.detach().item()
                epoch_kl += kl.detach().item()
                epoch_cl += cl.detach().item()

                # Count the number of edges
                adj = torch.where(generated_graph.detach() > 0.5, 1, 0)
                num_edges += (torch.count_nonzero(adj).detach().item()-15)/2

            epoch_loss /= len(val_dataset)
            epoch_recl /= len(val_dataset)
            epoch_kl /= len(val_dataset)
            epoch_cl /= len(val_dataset)
            num_edges /= len(val_dataset)
            y_true = np.array(y_true).flatten()
            y_pred = np.array(y_pred).flatten()
            pred_classes = np.where(y_pred.reshape((-1,)) > threshold, 1, 0)

            print('Validation epoch {}, loss {:.4f}'.format(epoch, epoch_loss))

            # Compute the roc_auc accuracy
            acc = f1_score(y_true.reshape((-1,)), pred_classes.reshape(-1,))
            print("Validation epoch {}, accuracy {:.4f}".format(epoch, acc))

            # Print average number of edges
            print("Validation epoch {}, num edges {:.4f}".format(epoch, num_edges))

            # Create and plot the confusion matrix
            conf_mat = confusion_matrix(y_true.reshape((-1,)), pred_classes)
            vis.heatmap(conf_mat, win='conf_mat', opts=dict(title="Confusion Matrix",
                                                            columnnames=['pred_0', 'pred_1'],
                                                            rownames=['true_0', 'true_1']))

            # Save the model only if validation accuracy has increased
            if acc > max_validation_acc:
                print("Accuracy increased. Saving model...")
                torch.save(classifier_model.state_dict(), '../../models/classifier_model.pt')
                torch.save(generator_model.state_dict(), '../../models/generator_model.pt')
                max_validation_acc = acc

                # Store the confusion matrix
                np.savetxt("../../models/confusion_matrix.csv", conf_mat, delimiter=",")

            vis.line(Y=torch.reshape(torch.tensor(epoch_loss), (-1,)), X=torch.reshape(torch.tensor(epoch), (-1,)),
                     update='append', win='train_loss', name='val_trace',
                     opts=dict(title="Validation Losses Per Epoch", xlabel="Epoch", ylabel="Loss"))

            vis.line(Y=torch.reshape(torch.tensor(acc), (-1,)), X=torch.reshape(torch.tensor(epoch), (-1,)),
                     update='append', win='train_acc', name='val_trace',
                     opts=dict(title="Validation Accuracy Per Epoch", xlabel="Epoch", ylabel="Accuracy"))

            vis.line(Y=torch.reshape(torch.tensor(epoch_recl), (-1, )), X=torch.reshape(torch.tensor(epoch), (-1, )),
                     update='append', win='train_recl', name='val_trace',
                     opts=dict(title="Validation Reconstruction Losses Per Epoch", xlabel="Epoch", ylabel="Loss"))

            vis.line(Y=torch.reshape(torch.tensor(epoch_kl), (-1, )), X=torch.reshape(torch.tensor(epoch), (-1, )),
                     update='append', win='train_kl', name='val_trace',
                     opts=dict(title="Validation KL Losses Per Epoch", xlabel="Epoch", ylabel="Loss"))

            vis.line(Y=torch.reshape(torch.tensor(epoch_cl), (-1, )), X=torch.reshape(torch.tensor(epoch), (-1, )),
                     update='append', win='train_cl', name='val_trace',
                     opts=dict(title="Validation Classification Losses Per Epoch", xlabel="Epoch", ylabel="Loss"))

            vis.line(Y=torch.reshape(torch.tensor(num_edges), (-1, )), X=torch.reshape(torch.tensor(epoch), (-1, )),
                     update='append', win='train_num_edge', name='val_trace',
                     opts=dict(title="Average number of graph edges", xlabel="Epoch", ylabel="Number of Edges"))

            if history_path:
                f = open(history_path+"_val_losses.txt", "a")
                f.write(str(epoch_loss) + "\n")
                f.close()

                f = open(history_path + "_val_accs.txt", "a")
                f.write(str(acc) + "\n")
                f.close()

                # Draw and save the graph every 500 iterations
                if epoch % 3 == 0:
                    adj = torch.where(generated_graph.detach() > 0.5, 1, 0)
                    nx_graph = nx.from_numpy_matrix(adj.numpy())
                    nx.draw(nx_graph, node_color=color_map, with_labels=True)
                    plt.savefig(history_path+"_val_graph_"+str(epoch)+".png", format="PNG")
                    plt.close()

        # Change LR
        # scheduler_gen.step()
        # scheduler_cl.step()


if __name__ == "__main__":
    train()

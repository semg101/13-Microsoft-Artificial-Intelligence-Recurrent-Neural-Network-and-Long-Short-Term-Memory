from matplotlib import pyplot as plt
import math
import numpy as np
import os
import pandas as pd
import random
import time

import cntk as C

try:
    from urllib.request import urlretrieve
except ImportError:
    from urllib import urlretrieve

# to make things reproduceable, seed random
np.random.seed(0)

# Select the right target device when this notebook is being tested:------------------------------------------1
'''
if 'TEST_DEVICE' in os.environ:
    if os.environ['TEST_DEVICE'] == 'cpu':
        C.device.try_set_default_device(C.device.cpu())
    else:
        C.device.try_set_default_device(C.device.gpu(0))

# Test for CNTK version-------------------------------------------------------------------------------------2
if not C.__version__ == "2.0":
    raise Exception("this lab is designed to work with 2.0. Current Version: " + C.__version__) 
'''

#There are two training modes that we can choose from for this lab:

#Fast mode: isFast is set to True. This is the default mode for the notebooks, which means we train for fewer iterations or train / test on limited data. 
#This ensures functional correctness of the notebook though the models produced are far from what a completed training would produce.

#Slow mode: We recommend the user to set this flag to False once the user has gained familiarity with the notebook content and wants to gain 
#insight from running the notebooks for a longer period with different parameters for training.

#For Fast mode we train the model for 100 epochs and results have low accuracy but is good enough for development. 
#The model yields good accuracy after 1000-2000 epochs.
isFast = True

# we need around 2000 epochs to see good accuracy. For testing 100 epochs will do.
EPOCHS = 200 if isFast else 2000


#Pre-processing
#Below, we define a generate_solar_data() function that performs the following tasks:

#read raw data into a pandas dataframe
#normalize the data
#group the data by day
#append the columns "solar.current.max" and "solar.total.max"
#generate the sequences for each day



#Training / Testing / Validation data preparation-----------------------------------------------------------------------------------2

#We start by reading the CSV file. The raw data is sorted by time. Normally, we would randomize the data before splitting into training, 
#validation and test datasets, but this would make it impractical to visualize results.

#Hence, we split the dataset in the following manner: pick the following (in sequence order): 8 values for training, 1 for validation and 
#1 for test until there is no more data. This will spread training, validation and test datasets across the full timeline while preserving time order.

def generate_solar_data(input_url, time_steps, normalize=1, val_size=0.1, test_size=0.1):
    """
    generate sequences to feed to rnn based on data frame with solar panel data
    the csv has the format: time ,solar.current, solar.total
     (solar.current is the current output in Watt, solar.total is the total production
      for the day so far in Watt hours)
    """
    # try to find the data file local. If it doesn't exists download it.
    cache_path = os.path.join("data", "iot")
    cache_file = os.path.join(cache_path, "solar.csv")
    if not os.path.exists(cache_path):
        os.makedirs(cache_path)
    if not os.path.exists(cache_file):
        urlretrieve(input_url, cache_file)
        print("downloaded data successfully from ", input_url)
    else:
        print("using cache for ", input_url)
    
    df = pd.read_csv(cache_file, index_col="time", parse_dates=['time'], dtype=np.float32)
    
    df["date"] = df.index.date
    
    # normalize data
    df['solar.current'] /= normalize
    df['solar.total'] /= normalize
    
    # group by day, find the max for a day and add a new column .max
    grouped = df.groupby(df.index.date).max()
    grouped.columns = ["solar.current.max", "solar.total.max", "date"]

    # merge continuous readings and daily max values into a single frame
    df_merged = pd.merge(df, grouped, right_index=True, on="date")
    df_merged = df_merged[["solar.current", "solar.total",
                           "solar.current.max", "solar.total.max"]]
    # we group by day so we can process a day at a time.
    grouped = df_merged.groupby(df_merged.index.date)
    per_day = []
    for _, group in grouped:
        per_day.append(group)

    # split the dataset into train, validatation and test sets on day boundaries
    val_size = int(len(per_day) * val_size)
    test_size = int(len(per_day) * test_size)
    next_val = 0
    next_test = 0

    result_x = {"train": [], "val": [], "test": []}
    result_y = {"train": [], "val": [], "test": []}    

    # generate sequences a day at a time
    for i, day in enumerate(per_day):
        # if we have less than 8 datapoints for a day we skip over the
        # day assuming something is missing in the raw data
        total = day["solar.total"].values
        if len(total) < 8:
            continue
        if i >= next_val:
            current_set = "val"
            next_val = i + int(len(per_day) / val_size)
        elif i >= next_test:
            current_set = "test"
            next_test = i + int(len(per_day) / test_size)
        else:
            current_set = "train"
        max_total_for_day = np.array(day["solar.total.max"].values[0])
        for j in range(2, len(total)):
            result_x[current_set].append(total[0:j])
            result_y[current_set].append([max_total_for_day])
            if j >= time_steps:
                break
    # make result_y a numpy array
    for ds in ["train", "val", "test"]:
        result_y[ds] = np.array(result_y[ds])
    return result_x, result_y


#Data caching----------------------------------------------------------------------------------------------------------------------------------
# there are 14 lstm cells, 1 for each possible reading we get per day
TIMESTEPS = 14

# 20000 is the maximum total output in our dataset. We normalize all values with 
# this so our inputs are between 0.0 and 1.0 range.
NORMALIZE = 20000

X, Y = generate_solar_data("https://www.cntk.ai/jup/dat/solar.csv", TIMESTEPS, normalize=NORMALIZE)


#Utility for data fetching------------------------------------------------------------------------------------------------------------------------
#next_batch() yields the next batch for training. We use variable size sequences supported by CNTK and batches are a list of numpy arrays 
#where the numpy arrays have variable length.

#A standard practice is to shuffle batches with each epoch. We don't do this here because we want to be able to graph the data that is easily interpretable.

# process batches of 10 days
BATCH_SIZE = TIMESTEPS * 10

def next_batch(x, y, ds):
    """get the next batch for training"""

    def as_batch(data, start, count):
        return data[start:start + count]

    for i in range(0, len(x[ds]), BATCH_SIZE):
        yield as_batch(X[ds], i, BATCH_SIZE), as_batch(Y[ds], i, BATCH_SIZE)

#Understand the data format
print(X['train'][0:3])
print(Y['train'][0:3])


#LSTM network setup-----------------------------------------------------------------------------------------------------------
#Define the size of the internal state
H_DIMS = 14               
def create_model(x):
    """Create the model for time series prediction"""
    with C.layers.default_options(initial_state = 0.1):
        m = C.layers.Recurrence(C.layers.RNNStep(H_DIMS))(x)
        m = C.sequence.last(m)
        m = C.layers.Dropout(0.2)(m)
        m = C.layers.Dense(1)(m)
        return m

#Training----------------------------------------------------------------------------------------------------------------------------
# input sequences
x = C.sequence.input_variable(1)

# create the model
z = create_model(x)

# expected output (label), also the dynamic axes of the model output
# is specified as the model of the label input
l = C.input_variable(1, dynamic_axes=z.dynamic_axes, name="y")

# the learning rate
learning_rate = 0.005
lr_schedule = C.learning_rate_schedule(learning_rate, C.UnitType.minibatch)

# loss function
loss = C.squared_error(z, l)

# use squared error to determine error for now
error = C.squared_error(z, l)

# use adam optimizer
momentum_time_constant = C.momentum_as_time_constant_schedule(BATCH_SIZE / -math.log(0.9)) 
learner = C.fsadagrad(z.parameters, 
                      lr = lr_schedule, 
                      momentum = momentum_time_constant)
trainer = C.Trainer(z, (loss, error), [learner])

#Time to start training.---------------------------------------------------------------------------------------------------------
# training
loss_summary = []

start = time.time()
for epoch in range(0, EPOCHS):
    for x_batch, l_batch in next_batch(X, Y, "train"):
        trainer.train_minibatch({x: x_batch, l: l_batch})
        
    if epoch % (EPOCHS / 10) == 0:
        training_loss = trainer.previous_minibatch_loss_average
        loss_summary.append(training_loss)
        print("epoch: {}, loss: {:.4f}".format(epoch, training_loss))

print("Training took {:.1f} sec".format(time.time() - start))

#A look how the loss function shows how the model is converging:-------------------------------------------------------------------
plt.plot(loss_summary, label='training loss');
plt.show()

# evaluate the specified X and Y data on our model
def get_mse(X,Y,labeltxt):
    result = 0.0
    for x1, y1 in next_batch(X, Y, labeltxt):
        eval_error = trainer.test_minibatch({x : x1, l : y1})
        result += eval_error
    return result/len(X[labeltxt])

# Print the training and validation errors
for labeltxt in ["train", "val"]:
    print("mse for {}: {:.8f}".format(labeltxt, get_mse(X, Y, labeltxt)))

# Print the test error
labeltxt = "test"
print("mse for {}: {:.8f}".format(labeltxt, get_mse(X, Y, labeltxt)))


#Visualize the prediction---------------------------------------------------------------------------------------------------------------------
#Our model has been trained well, given that the training, validation and test errors are in the same ballpark. 
#To better understand our predictions, let's visualize the results. We will take our newly created model, make predictions 
#and plot them against the actual readings.

# predict
f, a = plt.subplots(2, 1, figsize=(12, 8))
for j, ds in enumerate(["val", "test"]):
    results = []
    for x_batch, _ in next_batch(X, Y, ds):
        pred = z.eval({x: x_batch})
        results.extend(pred[:, 0])
    # because we normalized the input data we need to multiply the prediction
    # with SCALER to get the real values.
    a[j].plot((Y[ds] * NORMALIZE).flatten(), label=ds + ' raw');
    a[j].plot(np.array(results) * NORMALIZE, label=ds + ' pred');
    a[j].legend();

plt.show()
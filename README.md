# DiseaseProgressionModeling-HMM
Code to implement a personalized input output hidden Markov model (PIOHMM) and other hidden Markov model variations. PIOHMMs are described in K.A. Severson, L.M. Chahine, L. Smolensky, K. Ng, J. Hu and S. Ghosh, 'Personalized Input-Output Hidden Markov Models for Disease Progression Modeling' MLHC 2020. Full details are available [here](https://static1.squarespace.com/static/59d5ac1780bd5ef9c396eda6/t/5f22cb86bc954f32697e42aa/1596115849139/65_CameraReadySubmission_MJFF_Methodological-5.pdf). The PIOHMM model class is in `piohmm.py`.

## Running the code
See the jupyter notebook 'Sample Model' for a simple example of the model. There are three primary components for using a PIOHMM: 
* `HMM` to specify the particular model; see `__init__` for a description of the options
* `learn_model` to perform inference
* `predict_sequence` to use the Viterbi algorithm to make state predictions


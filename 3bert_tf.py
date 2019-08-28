'''
基于双向LSTM和CRF的BioID track1

用训练预料和测试预料一起训练word2vec，使得词向量本身捕捉语义信息

思路：
1、转换为3tag标注问题（0：非实体，1：实体的首词，2：实体的内部词）；
2、获取对应输入的语言学特征（字符特征，词性，chunk，词典特征，大小写）
3、通过双向LSTM，直接对输入序列进行概率预测
4、通过CRF+viterbi算法获得最优标注结果；

之前遇到的问题:
https://github.com/google-research/bert/issues/146
errors: "logits must be 2-dimensional" while request the service with tensorflow serving, 
the final reason is the version of tensorflow serving is too low，change to a higher version 1.12.0 solved the error。

error: alueError: An operation has `None` for gradient.
关闭 self.trainable_weights 和 self.non_trainable_weights.

error: FailedPreconditionError
https://stackoverflow.com/questions/34001922/failedpreconditionerror-attempting-to-use-uninitialized-in-tensorflow/34013098

通过URL下载BERT均失败,改为手动下载
mkdir /tmp/moduleB
curl -L "https://tfhub.dev/google/bert_uncased_L-12_H-768_A-12/1?tf-hub-format=compressed" | tar -zxvC /tmp/moduleB
或直接在浏览器输入https://tfhub.dev/google/bert_uncased_L-12_H-768_A-12/1?tf-hub-format=compressed下载,记得翻墙!!

'''

# 设置numpy和Tensorflow的随机种子，置顶
from numpy.random import seed
seed(1337)
from tensorflow import set_random_seed
set_random_seed(1337)

import os
import random
import math
import pickle as pkl
import string
import time
from tqdm import tqdm
from keras.utils import to_categorical
from keras.callbacks import Callback
from keras.layers import *
# from keras.engine.topology import Layer
from keras.layers.core import Layer
from keras.layers.wrappers import TimeDistributed, Bidirectional
from keras.models import Model, load_model, Sequential
from keras.optimizers import *
# from keras.utils import plot_model
from keras.preprocessing.sequence import pad_sequences
from keras.regularizers import l2
from keras.callbacks import ModelCheckpoint, EarlyStopping, TensorBoard
import keras.backend as K
import tensorflow_hub as hub

# import sys
# sys.path.append("..")
# from keras_contrib.layers import CRF
from keraslayers.ChainCRF import ChainCRF
# from sample.keraslayers.crf_keras import CRF
from utils.helpers import createCharDict
from utils.callbacks import ConllevalCallback
from utils.tokenization import FullTokenizer
import numpy as np
import codecs
from math import ceil
from collections import OrderedDict

# set GPU memory
if 'tensorflow'==K.backend():
    import tensorflow as tf
    from keras.backend.tensorflow_backend import set_session

    # # # 方法1:显存占用会随着epoch的增长而增长,也就是后面的epoch会去申请新的显存,前面已完成的并不会释放,为了防止碎片化
    # config = tf.ConfigProto()
    # config.gpu_options.allow_growth = True  # 按需求增长
    # sess = tf.Session(config=config)
    # set_session(sess)

    # 方法2:只允许使用x%的显存,其余的放着不动
    config = tf.ConfigProto()
    config.gpu_options.per_process_gpu_memory_fraction = 0.6    # 按比例
    sess = tf.Session(config=config)
    set_session(sess)

use_token = True
use_chars = True
use_cap = True
use_pos = True
use_chunk = True
use_dict = False
use_ngram = False
use_bert = True

use_att = False
batch_normalization = False
highway = False

# Parameters of the network
word_emb_size = 200
char_emb_size = 50
cap_emb_size = 10
pos_emb_size = 25
chunk_emb_size = 10
dict_emb_size = 15
num_classes = 5

epochs = 15
if use_bert:
    batch_size = 8
else:
    batch_size = 16 # 8
dropout_rate = 0.5  # [0.5, 0.5]
optimizer = 'rmsprop'  # 'rmsprop'
learning_rate = 1e-3    # 1e-3  5e-4
decay_rate = learning_rate / epochs     # 1e-6

# BLSTM 隐层大小
lstm_size = [200]    
# CNN settings
feature_maps = [25, 25]
kernels = [2, 3]

max_f = 0
label2idx = {'O': 0, 'B-protein': 1, 'I-protein': 2, 'B-gene': 3, 'I-gene': 4}
idx2label = {0: 'O', 1: 'B-protein', 2: 'I-protein', 3: 'B-gene', 4: 'I-gene'}
max_seq_length = 427
print('修改句子最大长度:427')

context_encoder = ['BLSTM', 'stack-LSTM'][0]
label_decoder = ['softmax', 'crf', 'rnn'][1]

# 下载好的BERT预训练模型
bert_path = 'bert/moduleB'

datasDic = {'train':[], 'test':[]}
labelsDic = {'train':[], 'test':[]}



class BertLayer(Layer):
    def __init__(self, weights=None, n_fine_tune_layers=10, **kwargs):
        self.n_fine_tune_layers = n_fine_tune_layers
        self.trainable = True
        self.output_size = 768
        self.initial_weights = weights
        super(BertLayer, self).__init__(**kwargs)

    def build(self, input_shape):
        # module 可以通过URL或文件路径来引入
        # 可通过$ export TFHUB_CACHE_DIR=/my_module_cache来指定module的下载路径
        
        self.bert = hub.Module(
            bert_path,
            trainable=self.trainable,
            name="{}_module".format(self.name)
        )

        # 报错 “An operation has `None` for gradient.” error
        trainable_vars = self.bert.variables

        # Remove unused layers
        trainable_vars = [var for var in trainable_vars if not "/cls/" in var.name and not '/pooler/' in var.name]

        # Select how many layers to fine tune
        trainable_vars = trainable_vars[-self.n_fine_tune_layers:]

        # Add to trainable weights
        for var in trainable_vars:
            # self.trainable_weights.append(K.variable(var))
            self._trainable_weights.append(var)
            
        for var in self.bert.variables:
            if var not in self._trainable_weights:
                self._non_trainable_weights.append(var)

        super(BertLayer, self).build(input_shape)

    def call(self, inputs):
        # tf.cast 用于改变某个章量的数据类型
        inputs = [K.cast(x, dtype="int32") for x in inputs]
        input_ids, input_mask, segment_ids = inputs
        bert_inputs = dict(
            input_ids=input_ids, input_mask=input_mask, segment_ids=segment_ids
        )
        bert_outputs = self.bert(inputs=bert_inputs, signature="tokens", as_dict=True)
        pooled_output = bert_outputs['pooled_output']   # (batch_size, hidden_size)
        sequence_output = bert_outputs['sequence_output']   # (batch_size, sequence_lenth, hidden_size)

        return sequence_output

    def compute_output_shape(self, input_shape):
        return (None, max_seq_length, self.output_size)


def CNN(seq_length, length, feature_maps, kernels, x):
    '''字符向量学习'''
    concat_input = []
    for filters, size in zip(feature_maps, kernels):
        charsConv1 = TimeDistributed(Conv1D(filters, size, padding='same', activation='relu'))(x)
        charsPool = TimeDistributed(GlobalMaxPool1D())(charsConv1)
        # reduced_l = length - kernel + 1
        # conv = Conv2D(feature_map, (1, kernel), activation='tanh', data_format="channels_last")(x)
        # maxp = MaxPooling2D((1, reduced_l), data_format="channels_last")(conv)
        concat_input.append(charsPool)

    x = Concatenate()(concat_input)
    x = Reshape((seq_length, sum(feature_maps)))(x)
    return x


TIME_STEPS = 21
def buildModel():

    mergeLayers = []
    model_input = []

    if use_token:
        '''字向量,若为shape=(None,)则代表输入序列是变长序列'''
        tokens_input = Input(shape=(max_seq_length,), name='tokens_input', dtype='int32')  # batch_shape=(batch_size,
        tokens_emb = Embedding(input_dim=embedding_matrix.shape[0],  # 索引字典大小
                               output_dim=embedding_matrix.shape[1],  # 词向量的维度
                               weights=[embedding_matrix],
                               trainable=True,
                               # mask_zero=True,    # 若√则编译报错，CuDNNLSTM 不支持？
                               name='token_emd')(tokens_input)
        mergeLayers.append(tokens_emb)
        model_input.append(tokens_input)

    if use_chars:
        char2idx = createCharDict()
        char_embedding = np.zeros([len(char2idx)+1, char_emb_size])
        for key, value in char2idx.items():
            limit = math.sqrt(3.0 / char_emb_size)
            vector = np.random.uniform(-limit, limit, char_emb_size)
            char_embedding[value] = vector

        '''字符向量'''
        chars_input = Input(shape=(max_seq_length, word_maxlen,), name='chars_input', dtype='int32')
        chars_emb = TimeDistributed(Embedding(input_dim=char_embedding.shape[0],
                                              output_dim=char_embedding.shape[1],
                                              weights=[char_embedding],
                                              trainable=True,
                                              # mask_zero=True,
                                              name='char_emd'))(chars_input)
        # # 基于 LSTM+attention 的字符表示学习
        # chars_lstm_out = TimeDistributed(Bidirectional(CuDNNLSTM(units=char_emb_size, return_sequences=True,
        #                                                         kernel_regularizer=l2(1e-4),
        #                                                         bias_regularizer=l2(1e-4))))(chars_emb)
        # chars_attention = TimeDistributed(Permute((2, 1)))(chars_lstm_out)
        # chars_attention = TimeDistributed(Dense(TIME_STEPS, activation='softmax'))(chars_attention)
        # chars_attention = TimeDistributed(Permute((2, 1), name='attention_vec'))(chars_attention)
        # chars_attention = Multiply()([chars_lstm_out, chars_attention])
        # chars_attention = TimeDistributed(GlobalAveragePooling1D())(chars_attention)

        # chars_lstm_final = TimeDistributed(Bidirectional(CuDNNLSTM(units=char_emb_size, return_sequences=False,
        #                                                            kernel_regularizer=l2(1e-4),
        #                                                            bias_regularizer=l2(1e-4)),
        #                                                  merge_mode='concat'))(chars_emb)
        # chars_rep = Concatenate(axis=-1)([chars_attention, chars_lstm_final])

        # # 基于CNN的字符表示学习
        chars_rep = CNN(max_seq_length, word_maxlen, feature_maps, kernels, chars_emb)

        mergeLayers.append(chars_rep)
        model_input.append(chars_input)

    # Additional features

    if use_cap:
        cap_input = Input(shape=(max_seq_length,), name='cap_input')
        cap_emb = Embedding(input_dim=5,  # 索引字典大小
                            output_dim=cap_emb_size,  # pos向量的维度
                            trainable=True)(cap_input)
        mergeLayers.append(cap_emb)
        model_input.append(cap_input)

    if use_pos:
        pos_input = Input(shape=(max_seq_length,), name='pos_input')
        pos_emb = Embedding(input_dim=60,  # 索引字典大小
                            output_dim=pos_emb_size,  # pos向量的维度
                            trainable=True)(pos_input)
        mergeLayers.append(pos_emb)
        model_input.append(pos_input)

    if use_chunk:
        chunk_input = Input(shape=(max_seq_length,), name='chunk_input')
        chunk_emb = Embedding(input_dim=25,  # 索引字典大小
                              output_dim=chunk_emb_size,  # chunk 向量的维度
                              trainable=True)(chunk_input)
        mergeLayers.append(chunk_emb)
        model_input.append(chunk_input)

    if use_dict:
        # 加入词典特征
        dict_input = Input(shape=(max_seq_length,), name='dict_input')
        dict_emb = Embedding(input_dim=5,
                              output_dim=dict_emb_size,  # dict 向量的维度
                              trainable=True)(dict_input)
        dict_emb = Bidirectional(CuDNNLSTM(lstm_size[-1], return_sequences=True))(dict_emb)
        mergeLayers.append(dict_emb)
        model_input.append(dict_input)

    if use_ngram:
        # 加入词典特征
        ngram_input = Input(shape=(max_seq_length, 7,), name='ngram_input')
        ngram_rnn = Bidirectional(CuDNNLSTM(lstm_size[-1], return_sequences=True))(ngram_input)
        mergeLayers.append(ngram_rnn)
        model_input.append(ngram_input)

    if use_bert:
        # 加入BERT向量
        in_id = Input(shape=(max_seq_length,), name="input_ids", dtype='int32')
        in_mask = Input(shape=(max_seq_length,), name="input_masks", dtype='int32')
        in_segment =Input(shape=(max_seq_length,), name="segment_ids", dtype='int32')
        bert_inputs = [in_id, in_mask, in_segment]

        bert_output = BertLayer(n_fine_tune_layers=0)(bert_inputs)  # 0:全部调整
        mergeLayers.append(bert_output)
        model_input = model_input + bert_inputs

    # 拼接
    concat_input = concatenate(mergeLayers, axis=-1) if len(mergeLayers)>1 else mergeLayers[0]

    if batch_normalization:
        concat_input = BatchNormalization()(concat_input)
    if highway:
        for l in range(highway):
            concat_input = TimeDistributed(Highway(activation='tanh'))(concat_input)

    # Dropout on final input
    concat_input = Dropout(dropout_rate)(concat_input)

    if context_encoder=='BLSTM':
        # CuDNNLSTM 报错：errors_impl.UnknownError: Fail to find the dnn implementation. 解决升级cudnn>=7.2
        concat_input = Bidirectional(CuDNNLSTM(lstm_size[-1], return_sequences=True))(concat_input)
    elif context_encoder=='stack-LSTM':
        # 层叠=2+残差
        lstm_dim = int(concat_input.shape[-1])
        hidden_rep = CuDNNLSTM(lstm_dim, return_sequences=True)(concat_input)
        hidden_rep = add([concat_input, hidden_rep])
        hidden_rep2 = CuDNNLSTM(lstm_dim, return_sequences=True)(hidden_rep)
        concat_input = add([hidden_rep, hidden_rep2])
    else:
    	raise

    output = concat_input

    # ======================================================================= #

    output = TimeDistributed(Dense(lstm_size[-1], activation='relu', name='relu_layer'))(output)
    output = Dropout(0.5)(output)

    if label_decoder == 'crf':
        output = TimeDistributed(Dense(num_classes, name='final_layer'))(output)
        # crf = CRF(num_classes, sparse_target=False)  # 定义crf层，参数为True，自动mask掉最有一个标签
        # output = crf(output)  # 包装一下原来的tag_score
        # loss_function = crf.loss_function
        crf = ChainCRF(name='CRF')
        output = crf(output)
        loss_function = crf.loss
    elif label_decoder == 'softmax':
        output = TimeDistributed(Dense(num_classes, activation='softmax', name='final_layer'))(output)
        loss_function = 'categorical_crossentropy'
    elif label_decoder == 'rnn':
        print('换成 3bert_tf_rnnd.py 来跑!!')

    if optimizer.lower() == 'adam':
        opt = Adam(lr=learning_rate, clipvalue=1.)
    elif optimizer.lower() == 'nadam':
        opt = Nadam(lr=learning_rate, clipvalue=1.)
    elif optimizer.lower() == 'rmsprop':
        opt = RMSprop(lr=learning_rate, clipvalue=1.)
    elif optimizer.lower() == 'sgd':
        opt = SGD(lr=0.001, decay=1e-5, momentum=0.9, nesterov=True)
        # opt = SGD(lr=0.001, momentum=0.9, decay=0., nesterov=True, clipvalue=5)

    model = Model(inputs=model_input, outputs=output)
    model.compile(loss=loss_function,
                  optimizer=opt,
                  metrics=["accuracy"])     # crf.accuracy
    model.summary()

    # plot_model(model, to_file='result/model.png', show_shapes=True)
    return model


def initialize_vars(sess):
    # FailedPreconditionError
    sess.run(tf.local_variables_initializer())
    sess.run(tf.global_variables_initializer())
    sess.run(tf.tables_initializer())
    K.set_session(sess)


def data_generator():
    # Create datasets (Only take up to max_seq_length words for memory)
    for name in ['train', 'test']:
        with open('data/' + name + '.final.txt', encoding='utf-8') as f:
            data_sen = []
            labels_sen = []
            for line in f:
                if line == '\n':
                    datasDic[name].append(' '.join(data_sen[0:max_seq_length]))
                    labelsDic[name].append(labels_sen[:max_seq_length])
                    data_sen = []
                    labels_sen = []
                else:
                    token = line.replace('\n', '').split('\t')
                    word = token[0]
                    pos = token[1]
                    chunk = token[2]
                    dict = token[3]
                    label = token[-1]

                    labelIdx = label2idx.get(label) if label in label2idx else label2idx.get('O')
                    labelIdx = np.eye(len(label2idx))[labelIdx]

                    # 对单词进行清洗
                    # word = wordNormalize(word)
                    data_sen.append(word)
                    labels_sen.append(labelIdx.tolist())
                    
    datasDic['train'] = np.array(datasDic['train'], dtype=object)[:, np.newaxis]
    datasDic['test'] = np.array(datasDic['test'], dtype=object)[:, np.newaxis]
    
    class PaddingInputExample(object):
        """Fake example so the num input examples is a multiple of the batch size.
    When running eval/predict on the TPU, we need to pad the number of examples
    to be a multiple of the batch size, because the TPU requires a fixed batch
    size. The alternative is to drop the last batch, which is bad because it means
    the entire output data won't be generated.
    We use this class instead of `None` because treating `None` as padding
    battches could cause silent errors.
    """

    class InputExample(object):
        """A single training/test example for simple sequence classification."""

        def __init__(self, guid, text_a, text_b=None, label=None):
            """Constructs a InputExample.
        Args:
        guid: Unique id for the example.
        text_a: string. The untokenized text of the first sequence. For single
            sequence tasks, only this sequence must be specified.
        text_b: (Optional) string. The untokenized text of the second sequence.
            Only must be specified for sequence pair tasks.
        label: (Optional) string. The label of the example. This should be
            specified for train and dev examples, but not for test examples.
        """
            self.guid = guid
            self.text_a = text_a
            self.text_b = text_b
            self.label = label

    def create_tokenizer_from_hub_module():
        """Get the vocab file and casing info from the Hub module."""
        bert_module =  hub.Module(bert_path)
        tokenization_info = bert_module(signature="tokenization_info", as_dict=True)
        vocab_file, do_lower_case = sess.run(
            [
                tokenization_info["vocab_file"],
                tokenization_info["do_lower_case"],
            ]
        )

        return FullTokenizer(vocab_file=vocab_file, do_lower_case=do_lower_case)

    def convert_single_example(tokenizer, example, max_seq_length=256):
        """Converts a single `InputExample` into a single `InputFeatures`."""

        if isinstance(example, PaddingInputExample):
            input_ids = [0] * max_seq_length
            input_mask = [0] * max_seq_length
            segment_ids = [0] * max_seq_length
            label = 0
            return input_ids, input_mask, segment_ids, label

        tokens_a = tokenizer.tokenize(example.text_a)
        if len(tokens_a) > max_seq_length - 2:
            tokens_a = tokens_a[0 : (max_seq_length - 2)]

        tokens = []
        segment_ids = []
        tokens.append("[CLS]")
        segment_ids.append(0)
        for token in tokens_a:
            tokens.append(token)
            segment_ids.append(0)
        tokens.append("[SEP]")
        segment_ids.append(0)

        input_ids = tokenizer.convert_tokens_to_ids(tokens)

        # The mask has 1 for real tokens and 0 for padding tokens. Only real
        # tokens are attended to.
        input_mask = [1] * len(input_ids)

        # Zero-pad up to the sequence length.
        while len(input_ids) < max_seq_length:
            input_ids.append(0)
            input_mask.append(0)
            segment_ids.append(0)
            # example.label.append(np.array([0,0,0,0,0]).tolist())

        assert len(input_ids) == max_seq_length
        assert len(input_mask) == max_seq_length
        assert len(segment_ids) == max_seq_length

        return input_ids, input_mask, segment_ids, example.label

    def convert_examples_to_features(tokenizer, examples, max_seq_length=256):
        """Convert a set of `InputExample`s to a list of `InputFeatures`."""

        input_ids, input_masks, segment_ids, labels = [], [], [], []
        for example in tqdm(examples, desc="Converting examples to features"):
            input_id, input_mask, segment_id, label = convert_single_example(
                tokenizer, example, max_seq_length
            )
            input_ids.append(input_id)
            input_masks.append(input_mask)
            segment_ids.append(segment_id)
            labels.append(label)
        return (
            np.array(input_ids),
            np.array(input_masks),
            np.array(segment_ids),
            np.array(labels),
            # np.array(labels).reshape(-1, 1),
        )

    def convert_text_to_examples(texts, labels):
        """Create InputExamples"""
        InputExamples = []
        for text, label in zip(texts, labels):
            InputExamples.append(
                InputExample(guid=None, text_a=" ".join(text), text_b=None, label=label)
            )
        return InputExamples

    # Instantiate tokenizer
    tokenizer = create_tokenizer_from_hub_module()

    # Convert data to InputExample format
    train_examples = convert_text_to_examples(datasDic['train'], labelsDic['train'])
    test_examples = convert_text_to_examples(datasDic['test'], labelsDic['test'])

    # Convert to features
    (train_input_ids, train_input_masks, train_segment_ids, train_labels 
    ) = convert_examples_to_features(tokenizer, train_examples, max_seq_length=max_seq_length)
    (test_input_ids, test_input_masks, test_segment_ids, test_labels
    ) = convert_examples_to_features(tokenizer, test_examples, max_seq_length=max_seq_length)

    return (train_input_ids, train_input_masks, train_segment_ids, train_labels), (test_input_ids, test_input_masks, test_segment_ids, test_labels)


if __name__ == '__main__':

    # if use_token or use_chars:
    with open('data/train_ngram.pkl', "rb") as f:
        train_x, train_elmo, train_labels, train_char, train_cap, train_pos, train_chunk, train_dict, train_ngram = pkl.load(f)
    with open('data/test_ngram.pkl', "rb") as f:
        test_x, test_elmo, test_labels, test_char, test_cap, test_pos, test_chunk, test_dict, test_ngram = pkl.load(f)
    with open('embedding/emb_ngram.pkl', "rb") as f:
        embedding_matrix = pkl.load(f)
    with open('embedding/length_ngram.pkl', "rb") as f:
        word_maxlen, sent_maxlen = pkl.load(f)

    # sent_maxlen = max_seq_length
    # train_labels = pad_sequences(train_labels, maxlen=sent_maxlen, padding='post')
    # test_labels = pad_sequences(test_labels, maxlen=sent_maxlen, padding='post')

    # if use_bert:
    (train_input_ids, train_input_masks, train_segment_ids, train_labels), \
        (test_input_ids, test_input_masks, test_segment_ids, test_labels) = data_generator()
    train_labels = pad_sequences(train_labels, maxlen=max_seq_length, padding='post')
    test_labels = pad_sequences(test_labels, maxlen=max_seq_length, padding='post')
    print('Create datasets (Only take up to max_seq_length words for memory)')

    def get_data():
        dataSet = OrderedDict()
        dataSet['train'] = []
        dataSet['test'] = []
        if use_token:
            dataSet['train'].append(train_x)
            dataSet['test'].append(test_x)
        if use_cap:
            dataSet['train'].append(train_cap)
            dataSet['test'].append(test_cap)
        if use_pos:
            dataSet['train'].append(train_pos)
            dataSet['test'].append(test_pos)
        if use_chunk:
            dataSet['train'].append(train_chunk)
            dataSet['test'].append(test_chunk)
        if use_dict:
            dataSet['train'].append(train_dict)
            dataSet['test'].append(test_dict)
        if use_ngram:
            dataSet['train'].append(train_ngram)
            dataSet['test'].append(test_ngram)

        # pad the sequences with zero
        for key, value in dataSet.items():
            for i in range(len(value)):
                dataSet[key][i] = pad_sequences(value[i], maxlen=max_seq_length, padding='post')

        if use_chars:
            # pad the char sequences with zero list
            for j in range(len(train_char)):
                train_char[j] = train_char[j][:max_seq_length]
                if len(train_char[j]) < max_seq_length:
                    train_char[j].extend(np.asarray([[0] * word_maxlen] * (max_seq_length - len(train_char[j]))))
            for j in range(len(test_char)):
                test_char[j] = test_char[j][:max_seq_length]
                if len(test_char[j]) < max_seq_length:
                    test_char[j].extend(np.asarray([[0] * word_maxlen] * (max_seq_length - len(test_char[j]))))

            if len(dataSet['train'])>=1:
                dataSet['train'].insert(1, np.array(train_char))
                dataSet['test'].insert(1, np.array(test_char))
            else:
                dataSet['train'].append(np.array(train_char))
                dataSet['test'].append(np.array(test_char))

        if use_bert:
            dataSet['train'] = dataSet['train'] + [train_input_ids, train_input_masks, train_segment_ids]
            dataSet['test'] = dataSet['test'] + [test_input_ids, test_input_masks, test_segment_ids]

        print(dataSet['train'][0].shape) # (13696, 427)
        print(dataSet['test'][0].shape) # (13696, 427)
        print(train_labels.shape) # (13696, 427)
        return dataSet['train'], train_labels, dataSet['test'], test_labels

    ###===================================================================###


    train_data, train_labels, test_data, test_labels = get_data()
    model = buildModel()
    print('done! Model building....')
    # Instantiate variables
    initialize_vars(K.get_session())
    calculatePRF1 = ConllevalCallback([item[:] for item in test_data], test_labels[:], 0, idx2label, max_seq_length, max_f, batch_size)
    tensorBoard = TensorBoard(log_dir='./model',     # 保存日志文件的地址,该文件将被TensorBoard解析以用于可视化
                                histogram_freq=0)     # 计算各个层激活值直方图的频率(每多少个epoch计算一次)
    start_time = time.time()
    model.fit(x=[item[:] for item in train_data], y=train_labels[:],
                epochs=epochs,
                batch_size=batch_size,
                shuffle=True,
                callbacks=[calculatePRF1],)
            #   validation_data=([test_input_ids, test_input_masks, test_segment_ids], test_y))
    time_diff = time.time() - start_time
    print("%.2f sec for training (4.5)" % time_diff)
    print(model.metrics_names)


###===================================================================###

# 重新加载模型权重
# path = 'Model_weight_Best.h5'
# if os.path.exists(path):
#     model.load_weights(path)
#     print('模型加载成功')
#     predictions = model.predict(dataSet['test'], verbose=1)
#     y_pred = predictions.argmax(axis=-1)
#     print(len(test_x))
#     y_pred = y_pred[:len(test_x)]

#     with open('result/predictions.pkl', "wb") as f:
#         pkl.dump((y_pred), f, -1)

#     # from sample.utils.write_test_result import writeOutputToFile
#     # writeOutputToFile(r'data/test.final.txt', y_pred)

# #　脚本测试
# import csv
# import _test_nnet as m
# m.main(ner_result='result/predictions.pkl')
# os.system('python /home/administrator/PycharmProjects/keras_bc6_track1/sample/evaluation/BioID_scorer_1_0_3/scripts/bioid_score.py '
#         '--verbose 1 --force '
#         'ned/BioID_scorer_1_0_3/scripts/bioid_scores '
#         'embedding/test_corpus_20170804/caption_bioc '
#         'system1:embedding/test_corpus_20170804/prediction')

# res = []
# csv_file = 'ned/BioID_scorer_1_0_3/scripts/bioid_scores/corpus_scores.csv'
# with open(csv_file) as csvfile:
#     csv_reader = csv.reader(csvfile)  # 使用csv.reader读取csvfile中的文件
#     birth_header = next(csv_reader)  # 读取第一行每一列的标题
#     i = 0
#     for row in csv_reader:  # 将csv 文件中的数据保存到birth_data中
#         item = []
#         if i in [2, 8, 14, 20]:
#             res.append(row[14:17])
#         if i == 20:
#             res.append(row[17:])
#             break
#         i += 1
# with open('prf.txt', 'a') as f:
#     f.write('0')
#     # f.write('{}'.format(epoch))
#     f.write('\n')

#     for i in range(len(res)):
#         item = [one[:7] for one in res[i]]  # 取小数点后四位
#         if i == 0:
#             f.write('any, strict: ')
#         if i == 1:
#             f.write('any,overlap: ')
#         if i == 2:
#             f.write('normalized,strict: ')
#         if i == 3:
#             f.write('normalized,overlap: ')
#         f.write('\t'.join(item))
#         f.write('\n')
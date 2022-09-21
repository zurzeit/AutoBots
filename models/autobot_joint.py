import math

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from models.context_encoders import MapEncoderPtsMA


def init(module, weight_init, bias_init, gain=1):
    weight_init(module.weight.data, gain=gain)
    bias_init(module.bias.data)
    return module


class PositionalEncoding(nn.Module):
    def __init__(self, d_model, dropout=0.1, max_len=20):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).transpose(0, 1)
        self.register_buffer('pe', pe)

    def forward(self, x):
        '''
        :param x: must be (T, B, H)
        :return:
        '''
        x = x + self.pe[:x.size(0), :]
        return self.dropout(x)


class OutputModel(nn.Module):
    '''
    This class operates on the output of AutoBot-Joint's decoder representation. It produces the parameters of a
    bivariate Gaussian distribution and possibly predicts the yaw.
    '''
    """
    predict [x_mean, y_mean, x_sigma, y_sigma, rho, yaws] from 3 layers of linear
    """
    def __init__(self, d_k=64, predict_yaw=False):
        super(OutputModel, self).__init__()
        self.d_k = d_k
        self.predict_yaw = predict_yaw
        out_len = 5 ## [x_mean, y_mean, x_sigma, y_sigma, rho]
        if predict_yaw:
            out_len = 6 ## [x_mean, y_mean, x_sigma, y_sigma, rho, yaws]

        init_ = lambda m: init(m, nn.init.xavier_normal_, lambda x: nn.init.constant_(x, 0), np.sqrt(2))
        ## 3 layers of linear
        self.observation_model = nn.Sequential(
            init_(nn.Linear(self.d_k, self.d_k)), nn.ReLU(),
            init_(nn.Linear(self.d_k, self.d_k)), nn.ReLU(),
            init_(nn.Linear(self.d_k, out_len))
        )
        self.min_stdev = 0.01

    def forward(self, agent_latent_state):
        """agent_latent_state:[T,BK,dk]?? (by Jasper)"""
        T = agent_latent_state.shape[0]
        BK = agent_latent_state.shape[1]

        ## send to 3 layers of linear
        pred_obs = self.observation_model(agent_latent_state.reshape(-1, self.d_k)).reshape(T, BK, -1)
        ## ideally pred_obs:[x_mean,y_mean,x_sigma,y_sigma,rho] (by Jasper)
        x_mean = pred_obs[:, :, 0]
        y_mean = pred_obs[:, :, 1]
        x_sigma = F.softplus(pred_obs[:, :, 2]) + self.min_stdev
        y_sigma = F.softplus(pred_obs[:, :, 3]) + self.min_stdev
        rho = torch.tanh(pred_obs[:, :, 4]) * 0.9  # for stability   (How?? by Jasper)
        if self.predict_yaw:
            yaws = pred_obs[:, :, 5]  # for stability
            return torch.stack([x_mean, y_mean, x_sigma, y_sigma, rho, yaws], dim=2)
        else:
            return torch.stack([x_mean, y_mean, x_sigma, y_sigma, rho], dim=2)


class AutoBotJoint(nn.Module):
    '''
    AutoBot-Joint Class.
    '''
    def __init__(self, d_k=128, _M=5, c=5, T=30, L_enc=1, dropout=0.0, k_attr=2, map_attr=3, num_heads=16, L_dec=1,
                 tx_hidden_size=384, use_map_lanes=False, num_agent_types=None, predict_yaw=False):
        super(AutoBotJoint, self).__init__()

        ## some kind of parameter initialization(by Jasper)
        init_ = lambda m: init(m, nn.init.xavier_normal_, lambda x: nn.init.constant_(x, 0), np.sqrt(2))

        self.k_attr = k_attr
        self.map_attr = map_attr
        self.d_k = d_k
        self._M = _M  # num agents other then the main agent.
        self.c = c
        self.T = T
        self.L_enc = L_enc # num_layers inside the encoder??(by Jasper)
        self.dropout = dropout
        self.num_heads = num_heads## num heads in the multi-head attention module??(by Jasper)
        self.L_dec = L_dec ## num of layers inside the decoder??(by Jasper)
        self.tx_hidden_size = tx_hidden_size## context??(by Jasper)
        self.use_map_lanes = use_map_lanes
        self.predict_yaw = predict_yaw

        # INPUT ENCODERS
        self.agents_dynamic_encoder = nn.Sequential(init_(nn.Linear(self.k_attr, self.d_k)))# Fc-layer just like transformer(by Jasper)
            ## to model the agent dynamic
        # ============================== AutoBot-Joint ENCODER ==============================
        self.social_attn_layers = []
        self.temporal_attn_layers = []
        for _ in range(self.L_enc):
            ## Time Encoding(for each agent, do the attention(transformer) of vectors from each timestep ) (by Jasper)
            tx_encoder_layer = nn.TransformerEncoderLayer(d_model=self.d_k, nhead=self.num_heads,
                                                          dropout=self.dropout, dim_feedforward=self.tx_hidden_size)
            self.temporal_attn_layers.append(nn.TransformerEncoder(tx_encoder_layer, num_layers=2))

            ## Social Encoding(for each timestep, do the attention(transformer) of vectors from each agent )(by Jasper)
            tx_encoder_layer = nn.TransformerEncoderLayer(d_model=self.d_k, nhead=self.num_heads,
                                                          dropout=self.dropout, dim_feedforward=self.tx_hidden_size)
            self.social_attn_layers.append(nn.TransformerEncoder(tx_encoder_layer, num_layers=1))

        self.temporal_attn_layers = nn.ModuleList(self.temporal_attn_layers) ## should be a L element List(by Jasper)
        self.social_attn_layers = nn.ModuleList(self.social_attn_layers) ## should be a L element List(by Jasper)

        # ============================== MAP ENCODER ==========================
        # Pass(by Jasper) need to comeback
        if self.use_map_lanes:
            self.map_encoder = MapEncoderPtsMA(d_k=self.d_k, map_attr=self.map_attr, dropout=self.dropout)
            self.map_attn_layers = nn.MultiheadAttention(self.d_k, num_heads=self.num_heads, dropout=self.dropout)

        # ============================== AGENT TYPES Encoders ==============================
        ## don't know what is this about??(by Jasper)
        self.emb_agent_types = nn.Sequential(init_(nn.Linear(num_agent_types, self.d_k)))
        self.dec_agenttypes_encoder = nn.Sequential(
            init_(nn.Linear(2 * self.d_k, self.d_k)), nn.ReLU(),
            init_(nn.Linear(self.d_k, self.d_k))
        )

        # ============================== AutoBot-Joint DECODER ==============================
        ## important!!!(by Jasper)
        self.Q = nn.Parameter(torch.Tensor(self.T, 1, self.c, 1, self.d_k), requires_grad=True)## shape:(self.T, 1, self.c, 1, self.d_k)(by Jasper)
        nn.init.xavier_uniform_(self.Q)

        self.social_attn_decoder_layers = []
        self.temporal_attn_decoder_layers = []
        for _ in range(self.L_dec):
            ## Time encoding
            tx_decoder_layer = nn.TransformerDecoderLayer(d_model=self.d_k, nhead=self.num_heads,
                                                          dropout=self.dropout, dim_feedforward=self.tx_hidden_size)
            self.temporal_attn_decoder_layers.append(nn.TransformerDecoder(tx_decoder_layer, num_layers=2))
            ## Social encoding
            tx_encoder_layer = nn.TransformerEncoderLayer(d_model=self.d_k, nhead=self.num_heads,
                                                          dropout=self.dropout, dim_feedforward=self.tx_hidden_size)
            self.social_attn_decoder_layers.append(nn.TransformerEncoder(tx_encoder_layer, num_layers=1))

        self.temporal_attn_decoder_layers = nn.ModuleList(self.temporal_attn_decoder_layers)## should be L_dec elements
        self.social_attn_decoder_layers = nn.ModuleList(self.social_attn_decoder_layers)## should be L_dec elements

        # ============================== Positional encoder ==============================
        self.pos_encoder = PositionalEncoding(self.d_k, dropout=0.0)

        # ============================== OUTPUT MODEL ==============================
        self.output_model = OutputModel(d_k=self.d_k, predict_yaw=self.predict_yaw)
            ## turn agent latent feature into [x_mean, y_mean, x_sigma, y_sigma, rho, yaws]

        # ============================== Mode Prob prediction (P(z|X_1:t)) ==============================
        self.P = nn.Parameter(torch.Tensor(c, 1, 1, d_k), requires_grad=True)  # Appendix C.2.
        nn.init.xavier_uniform_(self.P)

        if self.use_map_lanes:
            self.mode_map_attn = nn.MultiheadAttention(self.d_k, num_heads=self.num_heads, dropout=self.dropout)

        self.prob_decoder = nn.MultiheadAttention(self.d_k, num_heads=self.num_heads, dropout=self.dropout)## don't know yet
        self.prob_predictor = init_(nn.Linear(self.d_k, 1))

        self.train()

    def generate_decoder_mask(self, seq_len, device):
        ''' For masking out the subsequent info. '''
        '''
        return like
            tensor([[False,  True,  True,  True,  True],
                    [False, False,  True,  True,  True],
                    [False, False, False,  True,  True],
                    [False, False, False, False,  True],
                    [False, False, False, False, False]])
        '''
        subsequent_mask = (torch.triu(torch.ones((seq_len, seq_len), device=device), diagonal=1)).bool()
        return subsequent_mask

    def process_observations(self, ego, agents):
        """
            ego: shape [B, T_obs, k_attr+1] with last values being the existence mask.
            agents: shape [B, T_obs, M-1, k_attr+1] with last values being the existence mask.
        
        output(by Jasper)
            ego_tensor: shape [B, T_obs, k_attr], remove the mask attribute from the ego.(by Jasper)
            opps_tensor: shape [B, T_obs, M-1, k_attr], remove the mask attribute from the agents.(by Jasper)
            opps_mask: shape [B, T_obs, M] only agent will be true.(by Jasper)
            env_masks: shape: [B, T_obs]. only ego will be shown.(by Jasper)


        """
        # ego stuff
        ego_tensor = ego[:, :, :self.k_attr]
        env_masks = ego[:, :, -1]# shape: [B, T_obs]

        # Agents stuff

        temp_masks = torch.cat((torch.ones_like(env_masks.unsqueeze(-1)), agents[:, :, :, -1]), dim=-1)#[B, T_obs,1] cat [B, T_obs, M-1]-> [B, T_obs, M]
            ## combine the ego mask and the agent mask?? (by Jasper)
        opps_masks = (1.0 - temp_masks).type(torch.BoolTensor).to(agents.device)  # only for agents.
        opps_tensor = agents[:, :, :, :self.k_attr]  # only opponent states

        return ego_tensor, opps_tensor, opps_masks, env_masks

    def temporal_attn_fn(self, agents_emb, agent_masks, layer):
        """
        Aside from the layer we already established in the __init__, in this function, we try to adjust the tensor dimension into a proper shape to 
        put into the layer.(by Jasper)
        """

        '''
        :param agents_emb: (T, B, N, H)
        :param agent_masks: (B, T, N)
        :return: (T, B, N, H)

            N: maybe is number of agents(by Jasper)
            H: maybe is the encoding feature length(by Jasper)
            layer: can be self.temporal_attn_layers[i]  -> ith transformerEncoder layer(by Jasper)
        '''
        T_obs = agents_emb.size(0)
        B = agent_masks.size(0)
        ## for the Time Encoding,(because batch_first=False) we try to move the sequence_len dimension to the first dimension. That is T in this case.
        ## Hence, no need to adjust the agents_emb dimension order.(by Jasper)
        agent_masks = agent_masks.permute(0, 2, 1).reshape(-1, T_obs) 
        agent_masks[:, -1][agent_masks.sum(-1) == T_obs] = False  # Ensure agent's that don't exist don't throw NaNs.
        agents_temp_emb = layer(self.pos_encoder(agents_emb.reshape(T_obs, B * (self._M + 1), -1)),
                                src_key_padding_mask=agent_masks)
        return agents_temp_emb.view(T_obs, B, self._M+1, -1)

    def social_attn_fn(self, agents_emb, agent_masks, layer):
        """
        Aside from the layer we already established in the __init__, in this function, we try to adjust the tensor dimension into a proper shape to 
        put into the layer.(by Jasper)
        """

        '''
        :param agents_emb: (T, B, N, H)
        :param agent_masks: (B, T, N)
        :return: (T, B, N, H)
        '''
        T_obs = agents_emb.size(0)
        B = agent_masks.size(0)
        ## for the Social Encoding,(because batch_first=False) we try to move the sequence_len dimension to the first dimension. That is N in this case.
        ## Hence, we need to adjust the agents_emb dimension order.(by Jasper)
        agents_emb = agents_emb.permute(2, 1, 0, 3).reshape(self._M + 1, B * T_obs, -1) # try to switch the time dimension to the last dimension
        agents_soc_emb = layer(agents_emb, src_key_padding_mask=agent_masks.view(-1, self._M+1))
        agents_soc_emb = agents_soc_emb.view(self._M+1, B, T_obs, -1).permute(2, 1, 0, 3)
        return agents_soc_emb

    def temporal_attn_decoder_fn(self, agents_emb, context, agent_masks, layer):
        '''
        :param agents_emb: (T, BK, N, H)
        :param context: (T_in, BK, N, H)
        :param agent_masks: (BK, T, N)
        :return: (T, BK, N, H)
        '''
        '''
        BK: means B*k
        '''
        T_obs = context.size(0)
        BK = agent_masks.size(0)

        ## important!!! (by Jasper)
        time_masks = self.generate_decoder_mask(seq_len=self.T, device=agents_emb.device)

        agent_masks = agent_masks.permute(0, 2, 1).reshape(-1, T_obs)# (BKN, T)
        agent_masks[:, -1][agent_masks.sum(-1) == T_obs] = False  # Ensure that agent's that don't exist don't make NaN. (don't understand by Jasper)
        agents_emb = agents_emb.reshape(self.T, -1, self.d_k)  # [T, BxKxN, self.d_k]
        context = context.view(-1, BK*(self._M+1), self.d_k) # [self.T, BK*(self._M+1), self.d_k]

        agents_temp_emb = layer(agents_emb, context, tgt_mask=time_masks, memory_key_padding_mask=agent_masks)
        agents_temp_emb = agents_temp_emb.view(self.T, BK, self._M+1, -1)

        return agents_temp_emb

    def social_attn_decoder_fn(self, agents_emb, agent_masks, layer):
        '''
        :param agents_emb: (T, BK, N, H)
        :param agent_masks: (BK, T, N)
        :return: (T, BK, N, H)
        '''
        B = agent_masks.size(0)
        agent_masks = agent_masks[:, -1:].repeat(1, self.T, 1).view(-1, self._M + 1)  # take last timestep of all agents.
        agents_emb = agents_emb.permute(2, 1, 0, 3).reshape(self._M + 1, B * self.T, -1)
        agents_soc_emb = layer(agents_emb, src_key_padding_mask=agent_masks)
        agents_soc_emb = agents_soc_emb.view(self._M + 1, B, self.T, -1).permute(2, 1, 0, 3)
        return agents_soc_emb

    def forward(self, ego_in, agents_in, roads, agent_types):
        '''
        :param ego_in: one agent called ego, shape [B, T_obs, k_attr+1] with last values being the existence mask.
        :param agents_in: other scene agents, shape [B, T_obs, M-1, k_attr+1] with last values being the existence mask.
        :param roads: [B, M, S, P, map_attr+1] representing the road network or
                      [B, 1, 1] if self.use_map_lanes is False.
        :param agent_types: [B, M, num_agent_types] one-hot encoding of agent types, with the first agent idx being ego.
        :return:
            pred_obs: shape [c, T, B, M, 5(6)] c trajectories for all agents with every point being the params of
                                        Bivariate Gaussian distribution (and the yaw prediction if self.predict_yaw).
            mode_probs: shape [B, c] mode probability predictions P(z|X_{1:T_obs})
        '''
        B = ego_in.size(0)

        # Encode all input observations
        ego_tensor, _agents_tensor, opps_masks, env_masks = self.process_observations(ego_in, agents_in)
        agents_tensor = torch.cat((ego_tensor.unsqueeze(2), _agents_tensor), dim=2)
        agents_emb = self.agents_dynamic_encoder(agents_tensor).permute(1, 0, 2, 3)

        # Process through AutoBot's encoder
        for i in range(self.L_enc):
            agents_emb = self.temporal_attn_fn(agents_emb, opps_masks, layer=self.temporal_attn_layers[i]) ## adjust the feature shape and put into the layer(Jasper)
            agents_emb = self.social_attn_fn(agents_emb, opps_masks, layer=self.social_attn_layers[i]) ## adjust the feature shape and put into the layer(Jasper)
            # agents_emb: (T, B, N, H) H: should be the feature_len, N: should be the number of agent (by Jasper)
        # Process map information
        if self.use_map_lanes:
            orig_map_features, orig_road_segs_masks = self.map_encoder(roads, agents_emb)
            map_features = orig_map_features.unsqueeze(2).repeat(1, 1, self.c, 1, 1).view(-1, B * self.c * (self._M+1), self.d_k)
            road_segs_masks = orig_road_segs_masks.unsqueeze(2).repeat(1, self.c, 1, 1).view(B * self.c * (self._M+1), -1)

        # Repeat the tensors for the number of modes.
        opps_masks_modes = opps_masks.unsqueeze(1).repeat(1, self.c, 1, 1).view(B*self.c, ego_in.shape[1], -1)
        context = agents_emb.unsqueeze(2).repeat(1, 1, self.c, 1, 1) #(T, B, self.c, N, H)
        context = context.view(ego_in.shape[1], B*self.c, self._M+1, self.d_k) #(T, B*self.c, N, H)


        ### combine Q and agent type to encode agents_dec_emb
        # embed agent types
        agent_types_features = self.emb_agent_types(agent_types).unsqueeze(1).\
            repeat(1, self.c, 1, 1).view(-1, self._M+1, self.d_k)
            # agent_types_feature: [B*self.c, M, self.d_k]
        agent_types_features = agent_types_features.unsqueeze(0).repeat(self.T, 1, 1, 1)
            # agent_types_feature: [self.T, B*self.c, M, self.d_k]
        # AutoBot-Joint Decoding
        dec_parameters = self.Q.repeat(1, B, 1, self._M+1, 1).view(self.T, B*self.c, self._M+1, -1)
            # shape: [self.T, B*self.c, self._M+1, self.d_k]
        dec_parameters = torch.cat((dec_parameters, agent_types_features), dim=-1)
            # shape: [self.T, B*self.c, self._M+1, self.d_k + self.d_k]
        dec_parameters = self.dec_agenttypes_encoder(dec_parameters)
            # shape: [self.T, B*self.c, self._M+1, self.d_k]
        agents_dec_emb = dec_parameters
            # shape: [self.T, B*self.c, self._M+1, self.d_k]
        
        for d in range(self.L_dec):
            ## combine map feature into agents_dec_emb_map(by Jasper)
            if self.use_map_lanes and d == 1:
                agents_dec_emb = agents_dec_emb.reshape(self.T, -1, self.d_k)
                agents_dec_emb_map = self.map_attn_layers(query=agents_dec_emb, key=map_features, value=map_features,
                                                          key_padding_mask=road_segs_masks)[0]
                agents_dec_emb = agents_dec_emb + agents_dec_emb_map
                agents_dec_emb = agents_dec_emb.reshape(self.T, B*self.c, self._M+1, -1)

            agents_dec_emb = self.temporal_attn_decoder_fn(agents_dec_emb, context, opps_masks_modes, layer=self.temporal_attn_decoder_layers[d])
            agents_dec_emb = self.social_attn_decoder_fn(agents_dec_emb, opps_masks_modes, layer=self.social_attn_decoder_layers[d])

        ## output distribution(by Jasper)
        out_dists = self.output_model(agents_dec_emb.reshape(self.T, -1, self.d_k))
        out_dists = out_dists.reshape(self.T, B, self.c, self._M+1, -1).permute(2, 0, 1, 3, 4)
        

        # ======================================(Jasper split line)=================================
        # I think above(including self.Q) is about the position prediction. Below is about the mode probability prediction.(by Jasper)
        
        # Mode prediction
        mode_params_emb = self.P.repeat(1, B, self._M+1, 1).view(self.c, -1, self.d_k)
        mode_params_emb = self.prob_decoder(query=mode_params_emb, key=agents_emb.reshape(-1, B*(self._M+1), self.d_k),
                                            value=agents_emb.reshape(-1, B*(self._M+1), self.d_k))[0]
        if self.use_map_lanes:
            orig_map_features = orig_map_features.view(-1, B*(self._M+1), self.d_k)
            orig_road_segs_masks = orig_road_segs_masks.view(B*(self._M+1), -1)
            mode_params_emb = self.mode_map_attn(query=mode_params_emb, key=orig_map_features, value=orig_map_features,
                                                 key_padding_mask=orig_road_segs_masks)[0] + mode_params_emb
        
        ## should be output the probability of this mode
        ## just a linear, output a scalar
        mode_probs = self.prob_predictor(mode_params_emb).squeeze(-1).view(self.c, B, self._M+1).sum(2).transpose(0, 1)
        mode_probs = F.softmax(mode_probs, dim=1)

        # return  # [c, T, B, M, 5], [B, c]
        return out_dists, mode_probs


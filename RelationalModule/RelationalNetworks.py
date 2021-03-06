import numpy as np
import copy
import torch 
import torch.nn as nn
import torch.nn.functional as F

debug = False

class ExtractEntities(nn.Module):
    """Parse raw RGB pixels into entieties (vectors of k_out dimensions)"""
    def __init__(self, k_out, k_in=3, vocab_size = 6, n_dim=3, kernel_size=2, stride=1, padding=0):
        super(ExtractEntities, self).__init__()
        assert k_out%2 == 0, "Please provide an even number of output kernels k_out"
        self.embed = nn.Embedding(vocab_size, n_dim)
        layers = []
        layers.append(nn.Conv2d(n_dim+k_in, k_out//2, kernel_size, stride, padding))
        layers.append(nn.ReLU())
        layers.append(nn.Conv2d(k_out//2, k_out, kernel_size, stride, padding))
        layers.append(nn.ReLU())
        self.net = nn.Sequential(*layers)
        
    def forward(self, state):
        """
        Accepts an input of shape (batch_size, k_in, linear_size, linear_size, )
        Returns a tensor of shape (batch_size, 2*k_out, linear_size, linear_size)
        """
        assert len(state) == 2, "Provide tuple (x1,x2) as input"
        x1, x2 = state
        if debug:
            print("\nx1.shape (before ExtractEntities): ", x1.shape)
            print("x2.shape (before ExtractEntities): ", x2.shape)
        if len(x1.shape) < 4:
            x1 = x1.unsqueeze(0)
            x2 = x2.unsqueeze(0)
        x1_embed = self.embed(x1)
        x1_embed = x1_embed.reshape(x1.shape[:3]+(-1,))
        x = torch.cat((x1_embed, x2), axis = 3)
        x = x.transpose(-1,1)
        x = x.transpose(-1,-2)
        if debug:
            print("x.shape (after Embedding and reshape): ", x.shape)
        x = self.net(x)
        if debug:
            print("x.shape (ExtractEntities): ", x.shape)
        return x

class Convolution(nn.Module):
    def __init__(self, k_out, k_in=3, kernel_size=2, stride=1, padding=0):
        super(Convolution, self).__init__()
        assert k_out%2 == 0, "Please provide an even number of output kernels k_out"
        layers = []
        layers.append(nn.Conv2d(k_in, k_out//2, kernel_size, stride, padding))
        layers.append(nn.ReLU())
        layers.append(nn.Conv2d(k_out//2, k_out, kernel_size, stride, padding))
        layers.append(nn.ReLU())
        self.net = nn.Sequential(*layers)
        
    def forward(self, x):
        """
        Accepts an input of shape (batch_size, k_in, linear_size, linear_size)
        Returns a tensor of shape (batch_size, 2*k_out, linear_size, linear_size)
        """
        if debug:
            print("x.shape (before Convolution): ", x.shape)
        if len(x.shape) <= 3:
            x = x.unsqueeze(0)
        x = self.net(x)
        if debug:
            print("x.shape (ExtractEntities): ", x.shape)
        return x
    
class PositionalEncoding(nn.Module):
    """
    Adds two extra channels to the feature dimension, indicating the spatial 
    position (x and y) of each cell in the feature map using evenly spaced values
    between −1 and 1. Then projects the feature dimension to n_features through a 
    linear layer.
    """
    def __init__(self, n_kernels, n_features):
        super(PositionalEncoding, self).__init__()
        self.projection = nn.Linear(n_kernels + 2, n_features)

    def forward(self, x):
        """
        Accepts an input of shape (batch_size, linear_size, linear_size, n_kernels)
        Returns a tensor of shape (linear_size**2, batch_size, n_features)
        """
        x = self.add_encoding2D(x)
        if debug:
            print("x.shape (After encoding): ", x.shape)
        x = x.view(x.shape[0], x.shape[1],-1)
        if debug:
            print("x.shape (Before transposing and projection): ", x.shape)
        x = self.projection(x.transpose(2,1))
        x = x.transpose(1,0)
        
        if debug:
            print("x.shape (PositionalEncoding): ", x.shape)
        return x
    
    @staticmethod
    def add_encoding2D(x):
        x_ax = x.shape[-2]
        y_ax = x.shape[-1]
        
        x_lin = torch.linspace(-1,1,x_ax)
        xx = x_lin.repeat(x.shape[0],y_ax,1).view(-1, 1, y_ax, x_ax).transpose(3,2)
        
        y_lin = torch.linspace(-1,1,y_ax).view(-1,1)
        yy = y_lin.repeat(x.shape[0],1,x_ax).view(-1, 1, y_ax, x_ax).transpose(3,2)
        
        if torch.cuda.is_available():
            device = torch.device("cuda")
        else:
            device = torch.device("cpu")
    
        x = torch.cat((x,xx.to(device),yy.to(device)), axis=1)
        return x
    
class PositionwiseFeedForward(nn.Module):
    """
    Applies 2 linear layers with ReLU and dropout layers
    only after the first layer.
    """
    def __init__(self, d_model, d_ff, dropout=0.1):
        super(PositionwiseFeedForward, self).__init__()
        self.w_1 = nn.Linear(d_model, d_ff)
        self.w_2 = nn.Linear(d_ff, d_model)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        return self.w_2(self.dropout(F.relu(self.w_1(x))))
        
class AttentionBlock(nn.Module):
    def __init__(self, n_features, n_heads, n_hidden=64, dropout=0.1):
        """
        Args:
          n_features: Number of input and output features. (d_model)
          n_heads: Number of attention heads in the Multi-Head Attention.
          n_hidden: Number of hidden units in the Feedforward (MLP) block. (d_k)
          dropout: Dropout rate after the first layer of the MLP and the two skip connections.
        """
        super(AttentionBlock, self).__init__()
        self.norm = nn.LayerNorm(n_features)
        self.dropout = nn.Dropout(dropout)
        self.attn = nn.MultiheadAttention(n_features, n_heads, dropout)
        self.ff = PositionwiseFeedForward(n_features, n_hidden, dropout)
        
    def forward(self, x, mask=None):
        """
        Args:
          x of shape (n_pixels**2, batch_size, n_features): Input sequences.
          mask of shape (batch_size, max_seq_length): Boolean tensor indicating which elements of the input
              sequences should be ignored.
        
        Returns:
          z of shape (max_seq_length, batch_size, n_features): Encoded input sequence.

        Note: All intermediate signals should be of shape (n_pixels**2, batch_size, n_features).
        """

        attn_output, attn_output_weights = self.attn(x,x,x, key_padding_mask=mask) # MHA step
        x_norm = self.dropout(self.norm(attn_output + x)) # add and norm
        z = self.ff(x_norm) # FF step
        return self.dropout(self.norm(z)) # add and norm

def clones(module, N):
    "Produce N identical layers."
    return [copy.deepcopy(module) for _ in range(N)]

class RelationalModule(nn.Module):
    """Implements the relational module from paper Relational Deep Reinforcement Learning"""
    def __init__(self, n_kernels=24, n_features=256, n_heads=4, n_attn_modules=2, n_hidden=64, dropout=0):
        """
        Parameters
        ----------
        n_kernels: int (default 24)
            Number of features extracted for each pixel
        n_features: int (default 256)
            Number of linearly projected features after positional encoding.
            This is the number of features used during the Multi-Headed Attention
            (MHA) blocks
        n_heads: int (default 4)
            Number of heades in each MHA block
        n_attn_modules: int (default 2)
            Number of MHA blocks
        """
        super(RelationalModule, self).__init__()
        
        enc_layer = AttentionBlock(n_features, n_heads, n_hidden=n_hidden, dropout=dropout)
        
        #encoder_layers = clones(enc_layer, n_attn_modules)
        encoder_layers = nn.ModuleList([enc_layer for _ in range(n_attn_modules)])
        self.net = nn.Sequential(
            PositionalEncoding(n_kernels, n_features),
            *encoder_layers)
        
        #if debug:
        #    print(self.net)
        
    def forward(self, x):
        """Expects an input of shape (batch_size, n_pixels, n_kernels)"""
        x = self.net(x)
        if debug:
            print("x.shape (RelationalModule): ", x.shape)
        return x

class FeaturewiseMaxPool(nn.Module):
    """Applies max pooling along a given axis of a tensor"""
    def __init__(self, pixel_axis):
        super(FeaturewiseMaxPool, self).__init__()
        self.max_along_axis = pixel_axis
        
    def forward(self, x):
        x, _ = torch.max(x, axis=self.max_along_axis)
        if debug:
            print("x.shape (FeaturewiseMaxPool): ", x.shape)
        return x
    
class FeaturewiseProjection(nn.Module):
    """Applies max pooling along a given axis of a tensor"""
    def __init__(self, n_pixels):
        super(FeaturewiseProjection, self).__init__()
        self.norm = nn.LayerNorm(n_pixels)
        self.projection = nn.Linear(n_pixels,1)
        
    def forward(self, x):
        if debug: print("x.shape (FeaturewiseProjection): ", x.shape)
        x = x.transpose(-1,0)
        shape = x.shape
        if debug: print("x.shape (FeaturewiseProjection): ", x.shape)
        x = self.projection(self.norm(x)).reshape(shape[0],shape[1])
        if debug: print("x.shape (FeaturewiseProjection): ", x.shape)
        x = x.transpose(-1,0)
        if debug: print("x.shape (FeaturewiseProjection): ", x.shape)
        return x
    
class ResidualLayer(nn.Module):
    """
    Implements residual layer. Use LayerNorm and ReLU activation before applying the layers.
    """
    def __init__(self, n_features, n_hidden):
        super(ResidualLayer, self).__init__()
        self.norm = nn.LayerNorm(n_features)
        self.w1 = nn.Linear(n_features, n_hidden)
        self.w2 = nn.Linear(n_hidden, n_features)

    def forward(self, x):
        out = F.relu(self.w1(self.norm(x)))
        out = self.w2(out)
        return out + x
    
    
class ResidualConvolutional(nn.Module):
    
    def __init__(self, linear_size, n_channels, hidden_channels=12, kernel_size=3):
        super(ResidualConvolutional, self).__init__()
        
        padding = (kernel_size - 1) // 2
        assert (kernel_size - 1) % 2 == 0, 'Provide odd kernel size to use this layer'
        
        self.net = nn.Sequential(
                                nn.LayerNorm((linear_size, linear_size)),
                                nn.Conv2d(n_channels, hidden_channels, kernel_size, stride=1, padding=padding),
                                nn.ReLU(),
                                nn.Conv2d(hidden_channels, n_channels, kernel_size, stride=1, padding=padding)
                                )
        
    def forward(self, x):
        out = self.net(x)
        out = out + x
        return out
    
    
class MultiplicativeLayer(nn.Module):
    def __init__(self, n_channels, info_channels, mask_channels, out_channels):
        super(MultiplicativeLayer, self).__init__()
        self.mask_channels = mask_channels
        
        self.info_linear = nn.Conv2d(n_channels, info_channels, kernel_size=1)
        self.mask_linear = nn.Conv2d(n_channels, mask_channels, kernel_size=1)
        self.conv1by1 = nn.Conv2d(info_channels*mask_channels, out_channels, kernel_size=1)
        
    def forward(self, x):
        info_layers = F.relu(self.info_linear(x))
        #mask_layers = torch.tanh(self.mask_linear(x)) 
        mask_layers = torch.sigmoid(self.mask_linear(x)) 
        out = []
        for m in range(self.mask_channels):
            out.append(info_layers*mask_layers[:,m,...].unsqueeze(1))
        out = torch.cat(out, axis=1)
        out = F.relu(self.conv1by1(out))
        return out
    
class MultiplicativeLayer_v1(nn.Module):
    def __init__(self, n_channels, info_channels, mask_channels, out_channels):
        super(MultiplicativeLayer_v1, self).__init__()
        self.mask_channels = mask_channels
        
        self.info_linear1by1 = nn.Conv2d(n_channels, info_channels, kernel_size=1)
        self.mask_linear1by1 = nn.Conv2d(n_channels, mask_channels, kernel_size=1)
        self.info_linear3by3 = nn.Conv2d(n_channels, info_channels, kernel_size=3, padding=1)
        self.mask_linear3by3 = nn.Conv2d(n_channels, mask_channels, kernel_size=3, padding=1)
        self.conv1by1 = nn.Conv2d(info_channels*mask_channels, out_channels, kernel_size=1)
        
    def forward(self, x):
        info_layers = F.relu(self.info_linear1by1(x) + self.info_linear3by3(x))
        mask_layers = torch.sigmoid(self.mask_linear1by1(x)+self.mask_linear3by3(x)) 
        out = []
        for m in range(self.mask_channels):
            out.append(info_layers*mask_layers[:,m,...].unsqueeze(1))
        out = torch.cat(out, axis=1)
        out = F.relu(self.conv1by1(out))
        return out
    
class PosEncoding(nn.Module):

    def __init__(self):
        super(PosEncoding, self).__init__()

    def forward(self, x):
        """
        Accepts an input of shape (batch_size, linear_size, linear_size, n_channels)
        """
        x_ax = x.shape[-2]
        y_ax = x.shape[-1]
        
        x_lin = torch.linspace(-1,1,x_ax)
        xx = x_lin.repeat(x.shape[0],y_ax,1).view(-1, 1, y_ax, x_ax).transpose(3,2)
        
        y_lin = torch.linspace(-1,1,y_ax).view(-1,1)
        yy = y_lin.repeat(x.shape[0],1,x_ax).view(-1, 1, y_ax, x_ax).transpose(3,2)
        
        if torch.cuda.is_available():
            device = torch.device("cuda")
        else:
            device = torch.device("cpu")
    
        x = torch.cat((x,xx.to(device),yy.to(device)), axis=1)
        return x

class MultiplicativeNet(nn.Module):
    def __init__(self, in_channels=3, info_channels=2, mask_channels=2):
        super(MultiplicativeNet, self).__init__()
        
        out_channels = info_channels*mask_channels
        self.out_channels = out_channels
        
        self.pos_enc = PosEncoding()
        self.multi_layer = MultiplicativeLayer(in_channels+2, info_channels, mask_channels)
        self.MLP = nn.Sequential(
                                nn.Linear(out_channels, out_channels),
                                nn.ReLU(),
                                nn.Linear(out_channels, out_channels),
                                nn.ReLU(),
                                nn.Linear(out_channels, out_channels),
                                nn.Sigmoid()
                                )
        
    def forward(self, x):
        if len(x.shape) < 4:
            x = x.unsqueeze(0)
        x = self.pos_enc(x)
        if debug: print("x.shape (pos enc): ", x.shape)
        
        #x = x.permute(0,2,3,1)
        #if debug: print("x.shape (permutation): ", x.shape)
            
        x = self.multi_layer(x)
        if debug: print("x.shape (multi layer): ", x.shape)
        
        x = x.reshape((x.shape[0], x.shape[1], -1))
        if debug: print("x.shape (after rehsape): ", x.shape)
        
        x, _ = torch.max(x, axis=-1)
        if debug: print("x.shape (after max): ", x.shape)
        
        x = self.MLP(x)
        if debug: print("x.shape (after MLP): ", x.shape)
            
        return x

class MultiplicativeBlock(nn.Module):
    def __init__(self, in_channels, out_channels, info_channels=None, mask_channels=None,
                    hidden_channels=None, kernel_size=3, stride=1, padding=0, version='v1'):
        super(MultiplicativeBlock, self).__init__()
        
        # Set adaptive parameters for multiplicative layer
        if info_channels is None:
            info_channels = in_channels
        if mask_channels is None:
            mask_channels = in_channels
        if hidden_channels is None:
            hidden_channels = 2*in_channels
        
        if version == 'v0':
            self.net = nn.Sequential( 
                     MultiplicativeLayer(in_channels, info_channels, mask_channels, hidden_channels),
                     nn.Conv2d(hidden_channels, out_channels, kernel_size, stride, padding),
                     nn.ReLU()  )
        elif version == 'v1':
            self.net = nn.Sequential( 
                     MultiplicativeLayer_v1(in_channels, info_channels, mask_channels, hidden_channels),
                     nn.Conv2d(hidden_channels, out_channels, kernel_size, stride, padding),
                     nn.ReLU()  )
        else:
            raise Exception("Version unknown. Please select v0 or v1.")
        
    def forward(self, x):
        x = self.net(x)
        return x

    debug=True

class MultiplicativeConvNet(nn.Module):
    
    def __init__(self, linear_size, in_channels=3, info_channels=6, mask_channels=4, hidden_channels=12, out_channels=[12,24], 
                 padding=1, max_pool_size=2, n_features=64, residual_hidden_dim=64, n_residual_layers=2, 
                 version='v1', plug_off=False):
        
        super(MultiplicativeConvNet, self).__init__()

        n_multi_blocks = len(out_channels)
        self.out_channels = out_channels[-1]
        self.out_size = self.compute_out_size(linear_size, n_multi_blocks, max_pool_size, padding)
        self.n_features = n_features
        if debug:
            print("Out channels after forward1: ", self.out_channels)
            print("Linear size after forward1: ", self.out_size)
            
        if plug_off:
            self.forward1 = nn.Sequential(PosEncoding(), 
                                          nn.Conv2d(in_channels+2, out_channels[-1], 3, 1, 1),
                                          nn.ReLU(),
                                          ResidualConvolutional(linear_size, out_channels[-1], hidden_channels),
                                          nn.ReLU(),
                                          ResidualConvolutional(linear_size, out_channels[-1], hidden_channels),
                                          nn.ReLU()
                                          )
            
        else:
            multi_blocks = nn.ModuleList(
                [MultiplicativeBlock(in_channels+2, out_channels[0], info_channels, 
                                     mask_channels, hidden_channels, padding=padding, version=version)]+
                [MultiplicativeBlock(out_channels[i], out_channels[i+1], info_channels, 
                                     mask_channels, hidden_channels, padding=padding, version=version) 
                 for i in range(n_multi_blocks-1)])
            
            self.forward1 = nn.Sequential( PosEncoding(), *multi_blocks)
            
        self.maxpool = nn.MaxPool2d(max_pool_size)

        residual_MLP = nn.ModuleList([ResidualLayer(n_features, residual_hidden_dim)
                                      for _ in range(n_residual_layers)])
        self.forward2 = nn.Sequential(nn.Linear(self.out_channels*self.out_size**2, n_features), *residual_MLP)
    
    def forward(self, x):
        if len(x.shape) < 4:
            x = x.unsqueeze(0)
        x = self.forward1(x)
        if debug: print("x.shape (after forward1): ", x.shape)
            
        x = self.maxpool(x)
        if debug: print("x.shape (after maxpool): ", x.shape)
            
        x = x.reshape((x.shape[0],-1))
        if debug: print("x.shape (after rehsape): ", x.shape)
        
        x = self.forward2(x)
        if debug: print("x.shape (after residual MLP): ", x.shape)
            
        return x
    
    @staticmethod
    def compute_out_size(linear_size, n_multi_blocks, max_pool_size, padding):
        size = (linear_size - (2-2*padding)*n_multi_blocks) // max_pool_size
        return size
    
class BoxWorldNet_v0(nn.Module):
    """
    Implements architecture for BoxWorld agent of the paper Relational Deep Reinforcement Learning.
    
    Architecture:
    - 2 Convolutional layers (2x2 kernel size, stride 1, first with 12 out channels, second with 24)
    - Positional Encoding layer (2 more channels encoding x,y pixels' positions) and then projecting the 26
      channels to 256
    - Relational module, with one or more attention blocks (MultiheadedAttention + PositionwiseFeedForward)
    - FeaturewiseMaxPool layer
    - Multi-layer Perceptron with some (defaul = 4) fully-connected layers
    
    """
    def __init__(self, in_channels=1, n_kernels=24, vocab_size = 117, n_dim=3,
                 n_features=256, n_heads=4, n_attn_modules=2, n_linears=4, max_pool=True, linear_size=14):
        """
        Parameters
        ----------
        in_channels: int (default 1)
            Number of channels of the input image (e.g. 3 for RGB)
        n_kernels: int (default 24)
            Number of features extracted for each pixel
        vocab_size: int (default 117)
            Range of integer values of the raw pixels
        n_dim: int (default 3)
            Embedding dimension for each pixel channel (1 channel for greyscale, 
            3 for RGB)
        n_features: int (default 256)
            Number of linearly projected features after positional encoding.
            This is the number of features used during the Multi-Headed Attention
            (MHA) blocks
        n_heads: int (default 4)
            Number of heades in each MHA block
        n_attn_modules: int (default 2)
            Number of MHA blocks
        n_linears: int (default 4)
            Number of fully-connected layers after the FeaturewiseMaxPool layer
        """
        super(BoxWorldNet_v0, self).__init__()
        
        self.n_features = n_features
        
        #MLP = clones(ResidualLayer(n_features, n_features), n_linears)
        MLP = nn.ModuleList([ResidualLayer(n_features, n_features) for _ in range(n_linears)])
        self.process_input = ExtractEntities(n_kernels, in_channels, vocab_size, n_dim)
        
        if max_pool:
            self.net = nn.Sequential(
                RelationalModule(n_kernels, n_features, n_heads, n_attn_modules),
                FeaturewiseMaxPool(pixel_axis = 0),
                *MLP)
        else:
            self.net = nn.Sequential(
                RelationalModule(n_kernels, n_features, n_heads, n_attn_modules),
                FeaturewiseProjection(int((linear_size-2)**2)),
                *MLP)
        
        if debug:
            print(self.net)
        
    def forward(self, state):
        x = self.process_input(state)
        x = self.net(x)
        if debug:
            print("x.shape (BoxWorldNet): ", x.shape)
        return x
    
    
class BoxWorldNet(nn.Module):
    """
    Implements architecture for BoxWorld agent of the paper Relational Deep Reinforcement Learning.
    
    Architecture:
    - 2 Convolutional layers (2x2 kernel size, stride 1, first with 12 out channels, second with 24)
    - Positional Encoding layer (2 more channels encoding x,y pixels' positions) and then projecting the 26
      channels to 256
    - Relational module, with one or more attention blocks (MultiheadedAttention + PositionwiseFeedForward)
    - FeaturewiseMaxPool layer
    - Multi-layer Perceptron with some (defaul = 4) fully-connected layers
    
    """
    def __init__(self, in_channels=3, n_kernels=24, n_features=32, n_heads=2, 
                 n_attn_modules=4, feature_hidden_dim=64, feature_n_residuals=4):
        """
        Parameters
        ----------
        in_channels: int (default 1)
            Number of channels of the input image (e.g. 3 for RGB)
        n_kernels: int (default 24)
            Number of features extracted for each pixel
        vocab_size: int (default 117)
            Range of integer values of the raw pixels
        n_dim: int (default 3)
            Embedding dimension for each pixel channel (1 channel for greyscale, 
            3 for RGB)
        n_features: int (default 256)
            Number of linearly projected features after positional encoding.
            This is the number of features used during the Multi-Headed Attention
            (MHA) blocks
        n_heads: int (default 4)
            Number of heades in each MHA block
        n_attn_modules: int (default 2)
            Number of MHA blocks
        n_linears: int (default 4)
            Number of fully-connected layers after the FeaturewiseMaxPool layer
        """
        super(BoxWorldNet, self).__init__()
        
        self.n_features = n_features
        
        #MLP = clones(ResidualLayer(n_features, feature_hidden_dim), feature_n_residuals)
        MLP = nn.ModuleList([ResidualLayer(n_features, feature_hidden_dim) for _ in range(feature_n_residuals)])
        
        self.net = nn.Sequential(
            Convolution(k_in=in_channels, k_out=n_kernels),
            RelationalModule(n_kernels, n_features, n_heads, n_attn_modules),
            FeaturewiseMaxPool(pixel_axis = 0),
            *MLP)

        
        if debug:
            print(self.net)
        
    def forward(self, x):
        x = self.net(x)
        if debug:
            print("x.shape (BoxWorldNet): ", x.shape)
        return x
        
        
        
class OheNet(nn.Module):
    def __init__(self, map_size, k_in=3, k_out=24, n_features=32, pixel_hidden_dim=128, 
                 pixel_n_residuals=4, feature_hidden_dim=64, feature_n_residuals=4):
        
        super(OheNet, self).__init__()
        
        self.n_features = n_features
        
        self.OHE_conv = Convolution(k_in=k_in, k_out=k_out)
        self.pos_enc = PositionalEncoding(n_kernels=k_out, n_features=n_features)

        pixel_res_layers = nn.ModuleList([ResidualLayer(map_size**2, pixel_hidden_dim) for _ in range(pixel_n_residuals)])
        self.pixel_res_block = nn.Sequential(*pixel_res_layers)

        self.maxpool = FeaturewiseMaxPool(pixel_axis=2)

        feature_res_layers = nn.ModuleList([ResidualLayer(n_features, feature_hidden_dim) for _ in range(feature_n_residuals)])
        self.feature_res_block = nn.Sequential(*feature_res_layers)
        
    def forward(self, x):
        """ Input shape (batch_dim, k_in, map_size+2, map_size+2) """
        
        x = self.OHE_conv(x)
        if debug: print("conv_state.shape: ", x.shape)
            
        x = self.pos_enc(x)
        if debug: print("After positional enc + projection: ", x.shape)
            
        x = x.permute(1,2,0)
        if debug: print("x.shape: ", x.shape)
            
        x = self.pixel_res_block(x) # Interaction between pixels feature-wise
        if debug: print("x.shape: ", x.shape)
            
        x = self.maxpool(x) # Feature-wise maxpooling
        if debug: print("x.shape: ", x.shape)
            
        x = self.feature_res_block(x) # Interaction between features -> final representation
        if debug: print("x.shape: ", x.shape)
        
        return x     
        

### GatedTransformer for the attention/relational block ###

class GRU_gating(nn.Module):
    def __init__(self, n_features):
        super(GRU_gating, self).__init__()
        self.Wr = nn.Linear(n_features*2, n_features, bias=False)
        self.Wz = nn.Linear(n_features*2, n_features, bias=True)
        self.Wg = nn.Linear(n_features*2, n_features, bias=False)
        
    def forward(self, x, y):
        xy = torch.cat([x, y], axis=-1)
        if debug: print("xy.shape: ", xy.shape)
            
        r = torch.sigmoid(self.Wr(xy))
        if debug: print("r.shape: ", r.shape)
            
        z = torch.sigmoid(self.Wz(xy))
        if debug: print("z.shape: ", z.shape)
            
        rx = r*x
        if debug: print("rx.shape: ", rx.shape)
            
        h = torch.tanh(self.Wg(torch.cat([rx, y], axis=-1)))
        if debug: print("h.shape: ", h.shape)
            
        g = (1-z)*x + z*h
        if debug: print("g.shape: ", g.shape)
            
        return g       

class GatedTransformerBlock(nn.Module):
    def __init__(self, n_features, n_heads, n_hidden=64, dropout=0.1):
        """
        Args:
          n_features: Number of input and output features. (d_model)
          n_heads: Number of attention heads in the Multi-Head Attention.
          n_hidden: Number of hidden units in the Feedforward (MLP) block. (d_k)
          dropout: Dropout rate after the first layer of the MLP and the two skip connections.
        """
        super(GatedTransformerBlock, self).__init__()
        self.norm = nn.LayerNorm(n_features)
        self.dropout = nn.Dropout(dropout)
        self.attn = nn.MultiheadAttention(n_features, n_heads, dropout)
        self.GRU_gate1 = GRU_gating(n_features)
        self.ff = PositionwiseFeedForward(n_features, n_hidden, dropout)
        self.GRU_gate2 = GRU_gating(n_features)
        
    def forward(self, x, mask=None):
        """
        Args:
          x of shape (n_pixels**2, batch_size, n_features): Input sequences.
          mask of shape (batch_size, max_seq_length): Boolean tensor indicating which elements of the input
              sequences should be ignored.
        
        Returns:
          z of shape (max_seq_length, batch_size, n_features): Encoded input sequence.

        Note: All intermediate signals should be of shape (n_pixels**2, batch_size, n_features).
        """
        
        # First submodule
        x_norm = self.norm(x) # LayerNorm to the input before entering submodule
        attn_output, attn_output_weights = self.attn(x_norm, x_norm, x_norm, key_padding_mask=mask) # MHA step
        x = self.dropout(self.GRU_gate1(x, attn_output)) # skip connection added
        
        # Second submodule
        x_norm = self.norm(x) # LayerNorm to the input before entering submodule
        z = self.ff(x_norm) # FF step
        return self.dropout(self.GRU_gate2(x, z)) # skip connection added

class GatedRelationalModule(nn.Module):
    """Implements the relational module from paper Relational Deep Reinforcement Learning"""
    def __init__(self, n_kernels=24, n_features=256, n_heads=4, n_attn_modules=2, n_hidden=64, dropout=0):
        """
        Parameters
        ----------
        n_kernels: int (default 24)
            Number of features extracted for each pixel
        n_features: int (default 256)
            Number of linearly projected features after positional encoding.
            This is the number of features used during the Multi-Headed Attention
            (MHA) blocks
        n_heads: int (default 4)
            Number of heades in each MHA block
        n_attn_modules: int (default 2)
            Number of MHA blocks
        """
        super(GatedRelationalModule, self).__init__()
        
        enc_layer = GatedTransformerBlock(n_features, n_heads, n_hidden=n_hidden, dropout=dropout)
        
        #encoder_layers = clones(enc_layer, n_attn_modules)
        encoder_layers = nn.ModuleList([enc_layer for _ in range(n_attn_modules)])
        self.net = nn.Sequential(
            PositionalEncoding(n_kernels, n_features),
            *encoder_layers)
        
        #if debug:
        #    print(self.net)
        
    def forward(self, x):
        """Expects an input of shape (batch_size, n_pixels, n_kernels)"""
        x = self.net(x)
        if debug:
            print("x.shape (RelationalModule): ", x.shape)
        return x
    
class GatedBoxWorldNet(nn.Module):
    """
    Implements architecture for BoxWorld agent of the paper Relational Deep Reinforcement Learning.
    
    Architecture:
    - 2 Convolutional layers (2x2 kernel size, stride 1, first with 12 out channels, second with 24)
    - Positional Encoding layer (2 more channels encoding x,y pixels' positions) and then projecting the 26
      channels to 256
    - Relational module, with one or more attention blocks (MultiheadedAttention + PositionwiseFeedForward)
    - FeaturewiseMaxPool layer
    - Multi-layer Perceptron with some (defaul = 4) fully-connected layers
    
    """
    def __init__(self, in_channels=3, n_kernels=24, n_features=32, n_heads=2, 
                 n_attn_modules=4, feature_hidden_dim=64, feature_n_residuals=4):
        """
        Parameters
        ----------
        in_channels: int (default 1)
            Number of channels of the input image (e.g. 3 for RGB)
        n_kernels: int (default 24)
            Number of features extracted for each pixel
        vocab_size: int (default 117)
            Range of integer values of the raw pixels
        n_dim: int (default 3)
            Embedding dimension for each pixel channel (1 channel for greyscale, 
            3 for RGB)
        n_features: int (default 256)
            Number of linearly projected features after positional encoding.
            This is the number of features used during the Multi-Headed Attention
            (MHA) blocks
        n_heads: int (default 4)
            Number of heades in each MHA block
        n_attn_modules: int (default 2)
            Number of MHA blocks
        n_linears: int (default 4)
            Number of fully-connected layers after the FeaturewiseMaxPool layer
        """
        super(GatedBoxWorldNet, self).__init__()
        
        self.n_features = n_features

        MLP = nn.ModuleList([ResidualLayer(n_features, feature_hidden_dim) for _ in range(feature_n_residuals)])
        
        self.net = nn.Sequential(
            Convolution(k_in=in_channels, k_out=n_kernels),
            GatedRelationalModule(n_kernels, n_features, n_heads, n_attn_modules),
            FeaturewiseMaxPool(pixel_axis = 0),
            *MLP)

        
        if debug:
            print(self.net)
        
    def forward(self, x):
        x = self.net(x)
        if debug:
            print("x.shape (BoxWorldNet): ", x.shape)
        return x
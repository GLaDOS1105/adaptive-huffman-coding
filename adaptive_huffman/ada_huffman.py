import collections
import itertools
import logging
import math
import operator
import os
import sys

from bitarray import bitarray, bits2bytes
from progress.bar import ShadyBar
import numpy as np

from .tree import Tree, NYT, exchange


class AdaptiveHuffman:
    def __init__(self, byte_seq, alphabet_range=(0, 255), dpcm=False):
        """Create an adaptive huffman encoder and decoder.

        Args:
            byte_seq (bytes): The bytes sequence to encode or decode.
            alphabet_range (tuple or integer): The range of alphabet
                inclusively.
        """

        self.byte_seq = byte_seq
        self.dpcm = dpcm

        # Get the first decimal number of all alphabets
        self._alphabet_first_num = min(alphabet_range)
        alphabet_size = abs(alphabet_range[0] - alphabet_range[1]) + 1
        # Select an `exp` and `rem` which meet `alphabet_size = 2**exp + rem`.
        # Get the largest `exp` smaller than `alphabet_size`.
        self.exp = alphabet_size.bit_length() - 1
        self.rem = alphabet_size - 2**self.exp

        # Initialize the current node # as the maximum number of nodes with
        # `alphabet_size` leaves in a complete binary tree.
        self.current_node_num = alphabet_size * 2 - 1

        self.tree = Tree(0, self.current_node_num, data=NYT)
        self.all_nodes = [self.tree]
        self.nyt = self.tree  # initialize the NYT reference

    def encode(self):
        """Encode the target byte sequence into compressed bit sequence by
        adaptive Huffman coding.

        Returns:
            bitarray: The compressed bitarray. Use `bitarray.tofile()` to save
                to file.
        """

        def to_fixed_code(dec):
            alphabet_idx = dec - (self._alphabet_first_num - 1)
            ret = bitarray(endian=sys.byteorder)
            if alphabet_idx <= 2 * self.rem:
                ret.frombytes(int2bytes(alphabet_idx - 1))
                return ret[:self.exp + 1] if sys.byteorder == 'little' else ret[-(self.exp + 1):]
            ret.frombytes(int2bytes(alphabet_idx - self.rem - 1))
            return ret[:self.exp] if sys.byteorder == 'little' else ret[-(self.exp):]

        def to_dpcm(seq):
            seq = list(seq)
            return ((item - seq[idx - 1]) & 0xff if idx else seq[idx] for idx, item in enumerate(seq))

        progressbar = ShadyBar('encoding', max=len(self.byte_seq),
                               suffix='%(percent).1f%% - %(elapsed_td)ss')

        if self.dpcm:
            self.byte_seq = tuple(to_dpcm(self.byte_seq))

        logging.getLogger(__name__).info('entropy: %f' %
                                         entropy(self.byte_seq))

        code = bitarray(endian=sys.byteorder)
        for symbol in self.byte_seq:
            fixed_code = to_fixed_code(symbol)
            result = self.tree.search(fixed_code)
            if result['first_appearance']:
                code.extend(result['code'])  # send code of NYT
                code.extend(fixed_code)  # send fixed code of symbol
            else:
                # send code which is path from root to the node of symbol
                code.extend(result['code'])
            self.update(fixed_code, result['first_appearance'])
            progressbar.next()

        # Add remaining bits length info at the beginning of the code in order
        # to avoid the decoder regarding the remaining bits as actual data. The
        # remaining bits length info require 3 bits to store the length. Note
        # that the first 3 bits are stored as big endian binary string.
        remaining_bits_length = (bits2bytes(
            code.length() + 3) * 8 - (code.length() + 3))
        for bit in '{:03b}'.format(remaining_bits_length)[::-1]:
            code.insert(0, False if bit == '0' else True)

        progressbar.finish()
        return code

    def decode(self):
        """Decode the target byte sequence which is encoded by adaptive Huffman
        coding.

        Returns:
            list: A list of integer representing the number of decoded byte
                sequence.
        """

        def from_dpcm(seq):
            return itertools.accumulate(seq, lambda x, y: (x + y) & 0xff)

        def read_bit(n):
            """For decoder, get the first n bits in `bit_seq` and move
            self.idx forward for n.
            """
            progressbar.next(n)
            bits = bit_seq[self.idx:self.idx + n]
            self.idx += n
            return bits

        bit_seq = bitarray(endian=sys.byteorder)
        bit_seq.frombytes(self.byte_seq)
        self.idx = 0  # index of bit sequence
        progressbar = ShadyBar('decoding', max=bit_seq.length(),
                               suffix='%(percent).1f%% - %(elapsed_td)ss')

        # Remove the remaining bits in the last of bit sequence generated by
        # bitarray.tofile() to fill up to complete byte size (8 bits). The
        # remaining bits length could be retrieved by reading the first 3 bits.
        # Note that the first 3 bits are stored as big endian binary string.
        remaining_bits_length = int(read_bit(3).to01(), 2)
        del bit_seq[-remaining_bits_length:]
        progressbar.next(remaining_bits_length)

        code = []
        while self.idx < bit_seq.length():
            current_node = self.tree  # go to root
            while current_node.left or current_node.right:
                bit = read_bit(1)[0]
                current_node = current_node.right if bit else current_node.left
            if current_node.data == NYT:
                first_appearance = True
                # Convert fixed code into integer
                bits = read_bit(self.exp)
                dec = ord(bits.tobytes())
                if dec < self.rem:
                    bits.extend(read_bit(1))
                    dec = ord(bits.tobytes())
                else:
                    dec += self.rem
                dec += 1 + (self._alphabet_first_num - 1)
                code.append(dec)
            else:
                # decode element corresponding to node
                first_appearance = False
                dec = current_node.data
                code.append(current_node.data)
            self.update(dec, first_appearance)
        progressbar.finish()
        return from_dpcm(code) if self.dpcm else code

    def update(self, data, first_appearance):

        def find_node_data(data):
            for node in self.all_nodes:
                if node.data == data:
                    return node
            raise KeyError(
                'Cannot find the target node with given data %s' % data)

        current_node = None
        while True:
            if first_appearance:
                current_node = self.nyt

                self.current_node_num -= 1
                new_external = Tree(1, self.current_node_num, data=data)
                current_node.right = new_external
                self.all_nodes.append(new_external)

                self.current_node_num -= 1
                self.nyt = Tree(0, self.current_node_num, data=NYT)
                current_node.left = self.nyt
                self.all_nodes.append(self.nyt)

                current_node.weight += 1
                current_node.data = None
                self.nyt = current_node.left
            else:
                if not current_node:
                    # First time as `current_node` is None.
                    current_node = find_node_data(data)
                node_max_num_in_block = max(
                    (n for n in self.all_nodes if n.weight == current_node.weight),
                    key=operator.attrgetter('num'))
                if (current_node != node_max_num_in_block
                        and node_max_num_in_block != current_node.parent):
                    exchange(current_node, node_max_num_in_block)
                    current_node = node_max_num_in_block
                current_node.weight += 1
            if not current_node.parent:
                break
            current_node = current_node.parent
            first_appearance = False


def entropy(byte_seq):
    counter = collections.Counter(byte_seq)
    ret = 0
    for count in counter.values():
        prob = count / sum(counter.values())
        ret += prob * math.log2(prob)
    return -ret


def int2bytes(x):
    # NOTE: Do NOT use int.to_byte() directly. Use this function instead.
    if x == 0:
        return b'\x00'
    return x.to_bytes((x.bit_length() + 7) // 8, sys.byteorder)


def compress(in_filename, out_filename, alphabet_range, dpcm):
    with open(in_filename, 'rb') as in_file:
        logging.getLogger(__name__).info('open file: "%s"' % in_filename)
        content = in_file.read()
        logging.getLogger(__name__).info('original size: %d bytes' %
                                         os.path.getsize(in_file.name))
    ada_huff = AdaptiveHuffman(content, alphabet_range, dpcm)
    code = ada_huff.encode()

    with open(out_filename, 'wb') as out_file:
        logging.getLogger(__name__).info('write file: "%s"' % out_filename)
        code.tofile(out_file)
    logging.getLogger(__name__).info('compressed size: %d bytes' %
                                     os.path.getsize(out_filename))


def extract(in_filename, out_filename, alphabet_range, dpcm):
    with open(in_filename, 'rb') as in_file:
        logging.getLogger(__name__).info('open file: "%s"' % in_filename)
        content = in_file.read()
        logging.getLogger(__name__).info('original size: %d bytes' %
                                         os.path.getsize(in_file.name))
    ada_huff = AdaptiveHuffman(content, alphabet_range, dpcm)
    code = ada_huff.decode()

    with open(out_filename, 'wb') as out_file:
        logging.getLogger(__name__).info('write file: "%s"' % out_filename)
        out_file.write(bytes(code))
    logging.getLogger(__name__).info('extract size: %d bytes' %
                                     os.path.getsize(out_filename))

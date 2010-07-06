#!/usr/bin/python
# -*- coding: utf-8 -*-

import os 
import sys
import ConfigParser
config = ConfigParser.RawConfigParser()
config.read("../../etc/whois-server.conf")
root_dir =  config.get('global','root')
sys.path.append(os.path.join(root_dir,config.get('global','lib')))

whois_db = os.path.join(root_dir, config.get('global','whois_db'))
unpack_dir = os.path.join(root_dir, config.get('whois_server','unpack_dir'))
use_tmpfs = int(config.get('whois_server','use_tmpfs'))


import re
import redis 
from abc import ABCMeta, abstractmethod

from helpers.files_splitter import *
from multiprocessing import Process

# key incremented for each new ip range
uniq_range_id = 'range_id'

class InitWhoisServer:
    """
    Generic functions to initialize the redis database for a particular whois server. 
    This class needs some variables: 
    - keys: the list of keys of the database 
        format: [[ '^key', [] ] , [ '^key', [] ] ... ]
    - archive_name: the name of the db dump, gzip compressed
    - dump_name: the name of the db dump, extracted
    """
    
    max_pending_keys = 100000
    pending_keys = 0
    
    __metaclass__ = ABCMeta    
    @abstractmethod
    def push_helper_keys(self, key, redis_key, entry):
        """
        Push all helper keys for a particular whois source
        for example: push a network corresponding to a particular entry
        """
        pass

    def __intermediate_sets(self, first_set, last_set, ipv4):
        intermediate = []
        if ipv4:
            intermediate = self.__intermediate_sets_v4(first_set, last_set)
        else:
            intermediate = self.__intermediate_sets_v6(first_set, last_set)
        return intermediate

    def push_entry(self, entry, redis_key, flag, subkey):
        if entry is not None:
            self.push_list_at_key(entry, redis_key, flag, subkey)
    
    
    def push_list_at_key(self, mylist, redis_key, flag, subkey):
        mylist = filter(None, mylist)
        mylist = list(set(mylist))
        main_key = redis_key + flag
        for elt in mylist:
            self.redis_whois_server.sadd(main_key, elt)
            self.total_keys +=1
#            self.redis_whois_server.sadd(elt + subkey, redis_key)

    def __intermediate_sets_v4(self, first_set, last_set):
        intermediate = []
        first_index = first_set.split('.')
        last_index = last_set.split('.')
        if first_index[0] != last_index[0]:
            # push each values between first and last (first and last excluded) 
            intermediate = self.__intermediate_between(int(first_index[0])+ 1 , int(last_index[0]) - 1)
            if first_index[1] == '0' and last_index[1] == '255':
                intermediate.append(first_index[0])
                intermediate.append(last_index[0])
            else:
                # push each values from first_index[0].first_index[1] to first_index[0].255
                intermediate += self.__intermediate_to_last(first_index[1], first_index[0] + '.')
                # push each values from last_index[0].0 to last_index[0].last_index[1]
                intermediate += self.__intermediate_to_last(last_index[1], last_index[0] + '.')
        elif first_index[0] == last_index[0] and first_index[1] == '0' and last_index[1] == '255':
            intermediate.append(first_index[0])
        elif first_index[1] != last_index[1]:
            # push each values between first and last (first and last excluded) 
            intermediate = self.__intermediate_between(int(first_index[1])+ 1 , int(last_index[1]) - 1, first_index[0] + '.')
            if first_index[2] == '0' and last_index[2] == '255':
                intermediate.append(first_index[0] + '.' + first_index[1])
                intermediate.append(first_index[0] + '.' + last_index[1])
            else:
                # push each values from first_index[0].first_index[1].first_index[2] to first_index[0].first_index[1].255
                intermediate += self.__intermediate_to_last(first_index[2], first_index[0] + '.' + first_index[1] + '.')
                # push each values from last_index[0].last_index[1].0 to last_index[0].last_index[1].last_index[2]
                intermediate += self.__intermediate_to_last(last_index[2], last_index[0] + '.' + last_index[1] + '.')
        elif first_index[1] == last_index[1] and first_index[2] == '0' and last_index[2] == '255':
            intermediate.append(first_index[0] + '.' + first_index[1])
        elif first_index[2] != last_index[2]:
            intermediate = self.__intermediate_between(int(first_index[2]) , int(last_index[2]), first_index[0] + '.' + first_index[1] + '.')
        elif first_index[2] == last_index[2]:
            intermediate.append(first_index[0] + '.' + first_index[1] + '.' + first_index[2])
        return intermediate
    
    def __intermediate_sets_v6(self, first_set, last_set):
        intermediate = []
        first_index = first_set.split(':')
        last_index = last_set.split(':')
        i = 0
        key = ''
        while first_index[i] == last_index[i]:
            if i > 0 :
                 key += ':'
            key += first_index[i]
            i += 1
            if i == len(first_index) or i == len(last_index):
                break
        if key == '':
            hex_first = int('0x' + first_index[0], 16)
            hex_last = int('0x' + last_index[0], 16)
            while hex_first <= hex_last:
                key_end = ('%X' % hex_first).lower()
                intermediate.append(key_end)
                hex_first += 1
        else:
            intermediate.append(key)
        return intermediate

    def __init__(self):
        self.total_keys = 0
        self.total_main_keys = 0
        if use_tmpfs:
            tmpfs_size = config.get('whois_server','tmpfs_size')
            if not os.path.ismount(unpack_dir):
#                print('Mount the tmpfs directory')
                os.popen('mount -t tmpfs -o size=' + tmpfs_size + ' tmpfs ' + unpack_dir)
        self.extracted = os.path.join(unpack_dir,self.dump_name)

    def split(self):
        self.fs = FilesSplitter(self.extracted, int(config.get('global','init_processes')))
        return self.fs.fplit()
    
    def prepare(self):
        archive = os.path.join(self.whois_db,self.archive_name)
        os.popen('gunzip -c ' + archive + ' > ' + self.extracted)
    
    def dispatch_by_key(self, file):
        entry = ''
        f = open(file)
        for line in f:
            if line == '\n':
                if len(entry) > 0 and re.match('^#', entry) is None:
                    first_word = '^' + re.findall('(^[^\s]*).*',entry)[0]
                    entries = self.keys.get(first_word, None)
                    if entries is not None:
                        entries.append(entry)
                    else:
                        pass
#                        print entry
                entry = ''
                self.pending_keys += 1
                if self.pending_keys >= self.max_pending_keys:
                    self.push_into_db()
            else :
                entry += line
        self.push_into_db()
    
    def clean_system(self):
        if use_tmpfs:
            if os.path.ismount(self.unpack_dir) is not None:
                print('Umount the tmpfs directory')
                os.popen('umount ' + self.unpack_dir)
        else:
#            os.unlink(extracted)
            pass
    
    # push each values from first_index[0].first_index[1] to first_index[0].255
    def __intermediate_to_last(self, first, main_str = ''):
        intermediate = []
        first = int(first)
        while first <= 255:
            intermediate.append(main_str + str(first))
            first += 1
        return intermediate

    # push each values from last_index[0].0 to last_index[0].last_index[1]
    def __intermediate_from_zero(self, last, main_str = ''):
        intermediate = []
        last = int(last)
        b = 0
        while b <= last:
            intermediate.append(main_str + str(b))
            b += 1
        return intermediate
    
    def __intermediate_between(self, first, last, main_str = ''):
        intermediate = []
        while first <= last:
            intermediate.append(main_str + str(first))
            first += 1
        return intermediate

    def push_range(self, first, last, net_key, ipv4):
        range_key = str(first.int()) + '_' + str(last.int())
        first = str(first)
        last = str(last)
        intermediate_sets = self.__intermediate_sets(first, last, ipv4)
        for intermediate_set in intermediate_sets:
            self.redis_whois_server.sadd(intermediate_set, range_key)
            self.total_keys +=1
        self.redis_whois_server.set(range_key, net_key)
        self.total_keys +=1

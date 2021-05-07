# SPDX-License-Identifier: MIT                                                
# Copyright (c) 2021 GPUFORT Advanced Micro Devices, Inc. All rights reserved.
#!/usr/bin/env python3
import json
import time
import sys

start_time = time.time()
with open('out.json', 'r') as openfile: 
    # Reading from json file 
    json_object = json.load(openfile) 
print("--- %s seconds ---" % (time.time() - start_time),file=sys.stderr)
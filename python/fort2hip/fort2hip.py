# SPDX-License-Identifier: MIT                                                
# Copyright (c) 2021 GPUFORT Advanced Micro Devices, Inc. All rights reserved.
import os
import utils
import copy
import logging

import addtoplevelpath
import fort2hip.model as model
import translator.translator as translator
import indexer.indexer as indexer
import indexer.indexertools as indexertools
import scanner.scanner as scanner

INDEXER_ERROR_CODE = 1000

fort2hipDir = os.path.dirname(__file__)
exec(open("{0}/fort2hip_options.py.in".format(fort2hipDir)).read())

def convertDim3(dim3,dimensions,doFilter=True):
     result = []
     specified = dim3
     if doFilter:
         specified = [ x for x in dim3 if type(x) != int or x < 1 ]
     for i,value in enumerate(specified):
          if i >= dimensions:
              break
          el = {}
          el["dim"]   = chr(ord("X")+i)
          el["value"] = value
          result.append(el)
     return result

# arg for kernel generator
# array is split into multiple args
def initArg(argName,fType,kind,qualifiers=[],cType="",isArray=False):
    fTypeFinal = fType
    if len(kind):
        fTypeFinal += "({})".format(kind)
    arg = {
      "name"            : argName.replace("%","_") ,
      "callArgName"     : argName,
      "qualifiers"      : qualifiers,
      "type"            : fTypeFinal,
      "origType"        : fTypeFinal,
      "cType"           : cType,
      "cSize"           : "",
      "cValue"          : "",
      "cSuffix"         : "", # TODO still needed?
      "isArray"         : isArray,
      "reductionOp"     : "",
      "bytesPerElement" : translator.bytes(fType,kind,default="-1")
    }
    if not len(cType):
        arg["cType"] = translator.convertToCType(fType,kind,"void")
    if isArray:
        arg["cType"] += " * __restrict__"
    return arg

def createArgumentContext(indexedVar,argName,deviceptrNames=[],isLoopKernelArg=False):
    """
    Create an argument context dictionary based on a indexed variable.

    :param indexedVar: A variable description provided by the indexer.
    :type indexedVar: STDeclaration
    :return: a dicts containing Fortran `type` and `qualifiers` (`type`, `qualifiers`), C type (`cType`), and `name` of the argument
    :rtype: dict
    """
    arg = initArg(argName,indexedVar["fType"],indexedVar["kind"],[ "value" ],"",indexedVar["rank"]>0)
    if indexedVar["parameter"] and not indexedVar["value"] is None:
        arg["cValue"] = indexedVar["value"] 
    lowerBoundArgs = []  # additional arguments that we introduce if variable is an array
    countArgs      = []
    macro          = None
    # treat arrays
    rank = indexedVar["rank"] 
    if rank > 0:
        if argName in deviceptrNames:
            arg["callArgName"] = "c_loc({})".format(argName)
        else: 
            arg["callArgName"] = scanner.devVarName(argName)
        arg["type"]       = "type(c_ptr)"
        arg["qualifiers"] = [ "value" ]
        for d in range(1,rank+1):
             # lower bounds
             boundArg = initArg("{}_lb{}".format(argName,d),"integer","c_int",["value","intent(in)"],"const int")
             boundArg["callArgName"] = "lbound({},{})".format(argName,d)
             lowerBoundArgs.append(boundArg)
             # number of elements per dimensions
             countArg = initArg("{}_n{}".format(argName,d),"integer","c_int",["value","intent(in)"],"const int")
             countArg["callArgName"] = "size({},{})".format(argName,d)
             countArgs.append(countArg)
        # create macro expression
        if isLoopKernelArg and not indexedVar["unspecifiedBounds"]:
            macro = { "expr" : indexedVar["indexMacro"] }
        else:
            macro = { "expr" : indexedVar["indexMacroWithPlaceHolders"] }
    return arg, lowerBoundArgs, countArgs, macro

def deriveKernelArguments(index, identifiers, localVars, loopVars, whiteList=[], isLoopKernelArg=False, deviceptrNames=[]):
    """
    #TODO how to handle struct members?
    """
    kernelArgs          = []
    unknownArgs         = []
    cKernelLocalVars    = []
    macros              = []
    localArgs           = []
    localCpuRoutineArgs = []
    inputArrays         = []

    def includeArgument(name):
        nameLower = name.lower().strip()
        if len(whiteList):
            return name in whiteList
        else:
            if nameLower.startswith("_"):
                return False
            else:
                return True
            # TODO hack. This should be filtered differently. These are local loop variables

    #print(identifiers) # TODO check; should be all lower case
    for name in identifiers: # TODO does not play well with structs
        if includeArgument(name):
            #foundDeclaration = name in loopVars # TODO rename loop variables to local variables; this way we can filter out local subroutine variables
            indexedVar,discovered = indexertools.searchIndexForVariable(index,name) # TODO treat implicit here
            argName = name
            if not discovered:
                 arg = initArg(name,"TODO declaration not found","",[],"TODO declaration not found")
                 unknownArgs.append(arg)
            else:
                arg, lowerBoundArgs, countArgs, macro = createArgumentContext(indexedVar,name,deviceptrNames)
                argName = name.lower().replace("%","_") # TODO
                # modify argument
                if argName in loopVars: # specific for loop kernels
                    arg["qualifiers"]=[]
                    localCpuRoutineArgs.append(arg)
                elif argName in localVars:
                    arg["qualifiers"]=[]
                    if indexedVar["rank"] > 0:
                        arg["cSize"] = indexedVar["totalCount"]
                    localCpuRoutineArgs.append(arg)
                    cKernelLocalVars.append(arg)
                else:
                    rank = indexedVar["rank"]
                    if rank > 0: # specific for cufLoopKernel
                        inputArrays.append({ "name" : name, "rank" : rank })
                        arg["cSize"]    = ""
                        dimensions = "dimension({0})".format(",".join([":"]*rank))
                        # Fortran size expression for allocate
                        fSize = []
                        for i in range(0,rank):
                            fSize.append("{lb}:{lb}+{siz}-1".format(\
                                lb=lowerBoundArgs[i]["name"],siz=countArgs[i]["name"]))
                        localCpuRoutineArgs.append(\
                          { "name" : name,
                            "type" : arg["origType"],
                            "qualifiers" : ["allocatable",dimensions,"target"],
                            "bounds" : ",".join(fSize),
                            "bytesPerElement" : arg["bytesPerElement"]
                          }\
                        )
                    kernelArgs.append(arg)
                    for countArg in countArgs:
                        kernelArgs.append(countArg)
                    for boundArg in lowerBoundArgs:
                        kernelArgs.append(boundArg)
                if not macro is None:
                    macros.append(macro)

    # remove unknown arguments that are actually bound variables
    for unkernelArg in unknownArgs:
        append = True
        for kernelArg in kernelArgs:
            if unkernelArg["name"].lower() == kernelArg["name"].lower():
                append = False
                break
        if append:
            kernelArgs.append(unkernelArg)

    return kernelArgs, cKernelLocalVars, macros, inputArrays, localCpuRoutineArgs
    
def updateContextFromLoopKernels(loopKernels,index,hipContext,fContext):
    """
    loopKernels is a list of STCufLoopKernel objects.
    hipContext, fContext are inout arguments for generating C/Fortran files, respectively.
    """
    hipContext["haveReductions"] = False
    for stkernel in loopKernels:
        parentTag     = stkernel._parent.tag()
        filteredIndex = indexertools.filterIndexByTag(index,parentTag)
       
        fSnippet = "".join(stkernel.lines())

        # translate and analyze kernels
        kernelParseResult = translator.parseLoopKernel(fSnippet,filteredIndex)

        kernelArgs, cKernelLocalVars, macros, inputArrays, localCpuRoutineArgs =\
          deriveKernelArguments(index,\
            kernelParseResult.identifiersInBody(),\
            kernelParseResult.localScalars(),\
            kernelParseResult.loopVars(),\
            [], True, kernelParseResult.deviceptrs())

        # general
        kernelName         = stkernel.kernelName()
        kernelLauncherName = stkernel.kernelLauncherName()
   
        # treat reductionVars vars / acc default(present) vars
        hipContext["haveReductions"] = False # |= len(reductionOps)
        kernelCallArgNames    = []
        cpuKernelCallArgNames = []
        reductions            = kernelParseResult.gangTeamReductions(translator.makeCStr)
        reductionVars         = []
        for arg in kernelArgs:
            name  = arg["name"]
            cType = arg["cType"]
            cpuKernelCallArgNames.append(name)
            isReductionVar = False
            for op,variables in reductions.items():
                if name.lower() in [var.lower() for var in variables]:
                    # modify argument
                    arg["qualifiers"].remove("value")
                    arg["cType"] = cType + "*"
                    # reductionVars buffer var
                    bufferName = "_d_" + name
                    var = { "buffer": bufferName, "name" : name, "type" : cType, "op" : op }
                    reductionVars.append(var)
                    # call args
                    kernelCallArgNames.append(bufferName)
                    isReductionVar = True
            if not isReductionVar:
                kernelCallArgNames.append(name)
                if type(stkernel) is scanner.STAccLoopKernel:
                    if len(arg["cSize"]):
                        stkernel.appendDefaultPresentVar(name)
            hipContext["haveReductions"] |= isReductionVar
        # C LoopKernel
        dimensions  = kernelParseResult.numDimensions()
        block = convertDim3(kernelParseResult.numThreadsInBlock(),dimensions)
        if not len(block):
            defaultBlockSize = DEFAULT_BLOCK_SIZES 
            block = convertDim3(defaultBlockSize[dimensions],dimensions)
        hipKernelDict = {}
        hipKernelDict["isLoopKernel"]          = True
        hipKernelDict["modifier"]              = "__global__"
        hipKernelDict["returnType"]            = "void"
        hipKernelDict["generateLauncher"]      = GENERATE_KERNEL_LAUNCHER
        hipKernelDict["generateCPULauncher"]   = GENERATE_KERNEL_LAUNCHER and GENERATE_CPU_KERNEL_LAUNCHER
        hipKernelDict["launchBounds"]          = "__launch_bounds__({})".format(DEFAULT_LAUNCH_BOUNDS)
        hipKernelDict["size"]                  = convertDim3(kernelParseResult.problemSize(),dimensions,doFilter=False)
        hipKernelDict["grid"]                  = convertDim3(kernelParseResult.numGangsTeamsBlocks(),dimensions)
        hipKernelDict["block"]                 = block
        hipKernelDict["gridDims"  ]            = [ "{}_grid{}".format(kernelName,x["dim"])  for x in block ] # grid might not be always defined
        hipKernelDict["blockDims"  ]           = [ "{}_block{}".format(kernelName,x["dim"]) for x in block ]
        hipKernelDict["kernelName"]            = kernelName
        hipKernelDict["macros"]                = macros
        hipKernelDict["cBody"]                 = kernelParseResult.cStr()
        hipKernelDict["fBody"]                 = utils.prettifyFCode(fSnippet)
        hipKernelDict["kernelArgs"]            = ["{} {}{}{}".format(a["cType"],a["name"],a["cSize"],a["cSuffix"]) for a in kernelArgs]
        hipKernelDict["kernelCallArgNames"]    = kernelCallArgNames
        hipKernelDict["cpuKernelCallArgNames"] = cpuKernelCallArgNames
        hipKernelDict["reductions"]            = reductionVars
        hipKernelDict["kernelLocalVars"]       = ["{} {}{}".format(a["cType"],a["name"],a["cSize"]) for a in cKernelLocalVars]
        hipKernelDict["interfaceName"]         = kernelLauncherName
        hipKernelDict["interfaceComment"]      = "" # kernelLaunchInfo.cStr()
        hipKernelDict["interfaceArgs"]         = hipKernelDict["kernelArgs"]
        hipKernelDict["interfaceArgNames"]     = [arg["name"] for arg in kernelArgs] # excludes the stream;
        hipKernelDict["inputArrays"]           = inputArrays
        #inoutArraysInBody                   = [name.lower for name in kernelParseResult.inoutArraysInBody()]
        #hipKernelDict["outputArrays"]       = [array for array in inputArrays if array.lower() in inoutArraysInBody]
        hipKernelDict["outputArrays"]          = inputArrays
        hipContext["kernels"].append(hipKernelDict)

        generateLauncher   = GENERATE_KERNEL_LAUNCHER
        if generateLauncher:
            # Fortran interface with automatic derivation of stkernel launch parameters
            fInterfaceDictAuto = {}
            fInterfaceDictAuto["cName"]    = kernelLauncherName + "_auto"
            fInterfaceDictAuto["fName"]    = kernelLauncherName + "_auto"
            fInterfaceDictAuto["type"]     = "subroutine"
            fInterfaceDictAuto["args"]     = [
              {"type" : "integer(c_int)", "qualifiers" : ["value", "intent(in)"], "name" : "sharedMem", "cSize" : "" },
              {"type" : "type(c_ptr)"   , "qualifiers" : ["value", "intent(in)"], "name" : "stream",   "cSize": ""},
            ]
            fInterfaceDictAuto["args"]    += kernelArgs
            fInterfaceDictAuto["argNames"] = [arg["name"] for arg in fInterfaceDictAuto["args"]]

            # for test
            fInterfaceDictAuto["doTest"]   = False # True
            fInterfaceDictAuto["testComment"] = ["Fortran implementation:"] + fSnippet.split("\n")
            #fInterfaceDictAuto["testComment"] = ["","Hints:","Device variables in scope:"] + ["".join(declared._lines).lower() for declared in deviceVarsInScope]

            #######################################################################
            # Feed argument names back to STLoopKernel for host code modification
            #######################################################################
            stkernel._kernelArgNames = [arg["callArgName"] for arg in kernelArgs]
            stkernel._gridFStr       = kernelParseResult.gridExpressionFStr()
            stkernel._blockFStr      = kernelParseResult.blockExpressionFStr()
            # TODO use indexer to check if block and dim expressions are actually dim3 types or introduce overloaded make_dim3 interface to hipfort
            stkernel._streamFStr     = kernelParseResult.stream()    # TODO consistency
            stkernel._sharedMemFstr  = kernelParseResult.sharedMem() # TODO consistency

            # Fortran interface with manual specification of stkernel launch parameters
            fInterfaceDictManual = copy.deepcopy(fInterfaceDictAuto)
            fInterfaceDictManual["cName"] = kernelLauncherName
            fInterfaceDictManual["fName"] = kernelLauncherName
            fInterfaceDictManual["args"] = [
              {"type" : "type(dim3)", "qualifiers" : ["intent(in)"], "name" : "grid", "cSize": ""},
              {"type" : "type(dim3)", "qualifiers" : ["intent(in)"], "name" : "block", "cSize": ""},
              {"type" : "integer(c_int)", "qualifiers" : ["value", "intent(in)"], "name" : "sharedMem", "cSize" : "" },
              {"type" : "type(c_ptr)"   , "qualifiers" : ["value", "intent(in)"], "name" : "stream",   "cSize": ""},
            ]
            fInterfaceDictManual["args"]    += kernelArgs
            fInterfaceDictManual["argNames"] = [arg["name"] for arg in fInterfaceDictManual["args"]]
            fInterfaceDictManual["doTest"]   = False

            # External CPU interface
            fCPUInterfaceDict = copy.deepcopy(fInterfaceDictAuto)
            fCPUInterfaceDict["fName"] = kernelLauncherName + "_cpu" 
            fCPUInterfaceDict["cName"] = kernelLauncherName + "_cpu"
            fCPUInterfaceDict["doTest"] = False

            # Internal CPU routine
            fCPURoutineDict = copy.deepcopy(fInterfaceDictAuto)
            fCPURoutineDict["fName"]    = kernelLauncherName + "_cpu1" 
            fCPURoutineDict["cName"]    = kernelLauncherName + "_cpu1"
            
            # rename copied modified args
            for i,val in enumerate(fCPURoutineDict["args"]):
                varName = val["name"]
                if val.get("isArray",False):
                    fCPURoutineDict["args"][i]["name"] = "d_{}".format(varName)

            fCPURoutineDict["argNames"] = [a["name"] for a in fCPURoutineDict["args"]]
            fCPURoutineDict["args"]    += localCpuRoutineArgs # ordering important
            # add mallocs, memcpys , frees
            prolog = ""
            epilog = ""
            for arg in localCpuRoutineArgs:
                 if len(arg.get("bounds","")): # is local Fortran array
                   localArray = arg["name"]
                   # device to host
                   prolog += "allocate({var}({bounds}))\n".format(var=localArray,bounds=arg["bounds"])
                   prolog += "CALL hipCheck(hipMemcpy(c_loc({var}),d_{var},{bpe}_8*SIZE({var}),hipMemcpyDeviceToHost))\n".format(var=localArray,bpe=arg["bytesPerElement"])
                   # host to device
                   epilog += "CALL hipCheck(hipMemcpy(d_{var},c_loc({var}),{bpe}_8*SIZE({var}),hipMemcpyHostToDevice))\n".format(var=localArray,bpe=arg["bytesPerElement"])
                   epilog += "deallocate({var})\n".format(var=localArray)
            fCPURoutineDict["body"] = prolog + fSnippet + epilog

            # Add all definitions to context
            fContext["interfaces"].append(fInterfaceDictManual)
            fContext["interfaces"].append(fInterfaceDictAuto)
            fContext["interfaces"].append(fCPUInterfaceDict)
            fContext["routines"].append(fCPURoutineDict)

# TODO check if this can be combined with other routine
def updateContextFromDeviceProcedures(deviceProcedures,index,hipContext,fContext):
    """
    deviceProcedures is a list of STProcedure objects.
    hipContext, fContext are inout arguments for generating C/Fortran files, respectively.
    """
    def beginOfBody_(lines):
        """
        starts from the begin
        """
        lineno = 0
        while(not "use" in lines[lineno].lower() and\
              not "implicit" in lines[lineno].lower() and\
              not "::" in lines[lineno].lower()):
            lineno += 1
        return lineno
    def endOfBody_(lines):
        """
        starts from the end
        """
        lineno = len(lines)-1
        while(not "end" in lines[lineno].lower()):
            lineno -= 1
        return lineno
    
    for stprocedure in deviceProcedures:
        indexRecord = stprocedure._indexRecord
        isFunction  = indexRecord["type"] == "function"
        
        fBody  = "".join(stprocedure._lines[beginOfBody_(stprocedure._lines):endOfBody_(stprocedure._lines)])
        #fBody  = utils.prettifyFCode(fBody)
        
        if isFunction:
            resultName = indexValue["resultName"]
            resultVar = next([var for var in indexRecord["variables"] if var["name"] == indexValue["resultName"]],None)
            if resultVar != None:
                resultType = resultVar["cType"]
                parseResult = translator.parseProcedureBody(fBody,indexRecord,resultVar["name"])
            else:
                msg = "could not identify return value for function ''"
                logging.getLogger("").error(msg) ; print("ERROR: "+msg,file=sys.stderr)
                sys.exit(INDEXER_ERROR_CODE)
        else:
            resultType = "void"
            parseResult = translator.parseProcedureBody(fBody,indexRecord)

        # TODO: look up functions and subroutines called internally and supply to parseResult before calling cStr()
        cBody = parseResult.cStr()
    
        ## general
        generateLauncher   = GENERATE_KERNEL_LAUNCHER and stprocedure.isKernelSubroutine()
        kernelName         = indexRecord["name"]
        kernelLauncherName = "launch_" + kernelName
        loopVars = []; localLValues = []

        # sort identifiers: put dummy args first
        # TODO(dominic): More detailed analysis what to do with non-dummy args
        dummyArgs = indexRecord["dummyArgs"]
        nonDummyArgs = []
        for indexedVar in indexRecord["variables"]:
            if indexedVar["name"] not in dummyArgs:  
                nonDummyArgs.append(indexedVar["name"])
        identifiers = dummyArgs + nonDummyArgs

        kernelArgs, cKernelLocalVars, macros, inputArrays, localCpuRoutineArgs =\
          deriveKernelArguments([ indexRecord ],identifiers,localLValues,loopVars,dummyArgs,False,deviceptrNames=[])
        #print(argNames)

        # C routine and C stprocedure launcher
        hipKernelDict = {}
        hipKernelDict["generateLauncher"]      = generateLauncher
        hipKernelDict["generateCPULauncher"]   = False
        hipKernelDict["modifier"]              = "__global__" if stprocedure.isKernelSubroutine() else "__device__"
        hipKernelDict["launchBounds"]          = "__launch_bounds__({})".format(DEFAULT_LAUNCH_BOUNDS) if stprocedure.isKernelSubroutine() else ""
        hipKernelDict["returnType"]            = resultType
        hipKernelDict["isLoopKernel"]          = False
        hipKernelDict["kernelName"]            = kernelName
        hipKernelDict["macros"]                = macros
        hipKernelDict["cBody"]                 = cBody
        hipKernelDict["fBody"]                 = "".join(stprocedure._lines)
        hipKernelDict["kernelArgs"] = []
        # device procedures take all C args as reference or pointer
        # kernel proceduers take all C args as value or (device) pointer
        for arg in kernelArgs:
            cType = arg["cType"]
            if not stprocedure.isKernelSubroutine() and not arg["isArray"]:
                cType += "&"
            hipKernelDict["kernelArgs"].append(cType + " " + arg["name"])
        hipKernelDict["kernelLocalVars"]       = ["{0} {1}{2} {3}".format(a["cType"],a["name"],a["cSize"],"= " + a["cValue"] if "cValue" in a else "") for a in cKernelLocalVars]
        hipKernelDict["interfaceName"]         = kernelLauncherName
        hipKernelDict["interfaceArgs"]         = hipKernelDict["kernelArgs"]
        hipKernelDict["interfaceComment"]      = ""
        hipKernelDict["interfaceArgNames"]     = [arg["name"] for arg in kernelArgs]
        hipKernelDict["inputArrays"]           = inputArrays
        #inoutArraysInBody                   = [name.lower for name in kernelParseResult.inoutArraysInBody()]
        #hipKernelDict["outputArrays"]       = [array for array in inputArrays if array.lower() in inoutArraysInBody]
        hipKernelDict["outputArrays"]          = inputArrays
        hipKernelDict["kernelCallArgNames"]    = hipKernelDict["interfaceArgNames"] # TODO(05/12/21): Normally this information must be passed to other kernels
        hipKernelDict["cpuKernelCallArgNames"] = hipKernelDict["interfaceArgNames"] 
        hipKernelDict["reductions"]            = []
        hipContext["kernels"].append(hipKernelDict)

        if generateLauncher:
            # Fortran interface with manual specification of kernel launch parameters
            fInterfaceDictManual = {}
            fInterfaceDictManual["cName"]       = kernelLauncherName
            fInterfaceDictManual["fName"]       = kernelLauncherName
            fInterfaceDictManual["testComment"] = ["Fortran implementation:"] + stprocedure._lines
            fInterfaceDictManual["type"]        = "subroutine"
            fInterfaceDictManual["args"]        = [
                {"type" : "type(dim3)", "qualifiers" : ["intent(in)"], "name" : "grid"},
                {"type" : "type(dim3)", "qualifiers" : ["intent(in)"], "name" : "block"},
                {"type" : "integer(c_int)", "qualifiers" : ["value", "intent(in)"], "name" : "sharedMem"},
                {"type" : "type(c_ptr)", "qualifiers" : ["value", "intent(in)"], "name" : "stream"},
            ]
            fInterfaceDictManual["args"]    += kernelArgs
            fInterfaceDictManual["argNames"] = [arg["name"] for arg in fInterfaceDictManual["args"]]
            fInterfaceDictManual["doTest"]   = True
            fContext["interfaces"].append(fInterfaceDictManual)
 
            #TODO(12/05/2021): Check if it makes sense to generate a CPU version of a global kernel

            ## External CPU interface
            #fCPUInterfaceDict = copy.deepcopy(fInterfaceDictManual)
            #fCPUInterfaceDict["fName"]  = kernelLauncherName + "_cpu" 
            #fCPUInterfaceDict["cName"]  = kernelLauncherName + "_cpu"
            #fCPUInterfaceDict["args"]   = kernelArgs
            #fCPUInterfaceDict["doTest"] = False

            ## Internal CPU routine
            #fCPURoutineDict = copy.deepcopy(fInterfaceDictManual)
            #fCPURoutineDict["fName"]    = kernelLauncherName + "_cpu1" 
            #fCPURoutineDict["cName"]    = kernelLauncherName + "_cpu1"
            #fCPURoutineDict["args"]     = kernelArgs
            #fCPURoutineDict["argNames"] = [arg["name"] for arg in fCPURoutineDict["args"]]

            ## rename copied modified args
            #for i,val in enumerate(fCPURoutineDict["args"]):
            #    varName = val["name"]
            #    if val.get("isArray",False):
            #        fCPURoutineDict["args"][i]["name"] = "d_{}".format(varName)

            #fCPURoutineDict["argNames"] = [a["name"] for a in fCPURoutineDict["args"]]
            #fCPURoutineDict["args"]    += localCpuRoutineArgs # ordering important
            ## add mallocs, memcpys , frees
            #prolog = ""
            #epilog = ""
            #for arg in localCpuRoutineArgs:
            #     if len(arg.get("bounds","")): # is local Fortran array
            #       localArray = arg["name"]
            #       # device to host
            #       prolog += "allocate({var}({bounds}))\n".format(var=localArray,bounds=arg["bounds"])
            #       prolog += "CALL hipCheck(hipMemcpy(c_loc({var}),d_{var},{bpe}_8*SIZE({var}),hipMemcpyDeviceToHost))\n".format(var=localArray,bpe=arg["bytesPerElement"])
            #       # host to device
            #       epilog += "CALL hipCheck(hipMemcpy(d_{var},c_loc({var}),{bpe}_8*SIZE({var}),hipMemcpyHostToDevice))\n".format(var=localArray,bpe=arg["bytesPerElement"])
            #       epilog += "deallocate({var})\n".format(var=localArray)
            #fCPURoutineDict["body"] = prolog + fBody + epilog

            # Add all definitions to context
            #fContext["interfaces"].append(fCPUInterfaceDict)
            #fContext["routines"].append(fCPURoutineDict)

def renderTemplates(outputFilePrefix,hipContext,fContext):
    # HIP kernel file
    #pprint.pprint(hipContext)
    hipImplementationFilePath = "{0}.kernels.hip.cpp".format(outputFilePrefix)
    model.HipImplementationModel().generateCode(hipImplementationFilePath,hipContext)
    utils.prettifyCFile(hipImplementationFilePath,CLANG_FORMAT_STYLE)
    msg = "created HIP C++ implementation file: ".ljust(40) + hipImplementationFilePath
    logger = logging.getLogger("")
    logger.info(msg) ; print(msg)

    # header files
    outputDir = os.path.dirname(hipImplementationFilePath)
    gpufortHeaderFilePath = outputDir + "/gpufort.h"
    model.GpufortHeaderModel().generateCode(gpufortHeaderFilePath)
    msg = "created gpufort main header: ".ljust(40) + gpufortHeaderFilePath
    logger = logging.getLogger("")
    logger.info(msg) ; print(msg)
    if hipContext["haveReductions"]:
        gpufortReductionsHeaderFilePath = outputDir + "/gpufort_reductions.h"
        model.GpufortReductionsHeaderModel().generateCode(gpufortReductionsHeaderFilePath)
        msg = "created gpufort reductions header file: ".ljust(40) + gpufortReductionsHeaderFilePath
        logger = logging.getLogger("")
        logger.info(msg) ; print(msg)

    if len(fContext["interfaces"]):
        # Fortran interface/testing module
        moduleFilePath = "{0}.kernels.f08".format(outputFilePrefix)
        model.InterfaceModuleModel().generateCode(moduleFilePath,fContext)
        #utils.prettifyFFile(moduleFilePath)
        msg = "created interface/testing module: ".ljust(40) + moduleFilePath
        logger.info(msg) ; print(msg)

        # TODO disable tests for now
        if False:
           # Fortran test program
           testFilePath = "{0}.kernels.TEST.f08".format(outputFilePrefix)
           model.InterfaceModuleTestModel().generateCode(testFilePath,fContext)
           #utils.prettifyFFile(testFilePath)
           msg = "created interface module test file: ".ljust(40) + testFilePath
           logger.info(msg)
           print(msg)

def createHipKernels(stree,index,kernelsToConvertToHip,outputFilePrefix,basename,generateCode):
    """
    :param stree:        [inout] the scanner tree holds nodes that store the Fortran code lines of the kernels
    :param generateCode: generate code or just feed kernel signature information
                         back to the scanner tree.
    :note The signatures of the identified kernels must be fed back to the 
          scanner tree even when no kernel files are written.
    """
    global FORTRAN_MODULE_PREAMBLE
    if not len(kernelsToConvertToHip):
        return
    
    def select(kernel):
        nonlocal kernelsToConvertToHip
        condition1 = not kernel._ignoreInS2STranslation
        condition2 = \
                kernelsToConvertToHip[0] == "*" or\
                kernel._lineno in kernelsToConvertToHip or\
                kernel.kernelName() in kernelsToConvertToHip
        return condition1 and condition2

    # Context for HIP implementation
    hipContext = {}
    hipContext["includes"] = [ "hip/hip_runtime.h", "hip/hip_complex.h" ]
    hipContext["kernels"] = []
    
    # Context for Fortran interface/implementation
    fContext = {}
    moduleName = basename.replace(".","_").replace("-","_") + "_kernels"
    fContext["name"]       = moduleName
    fContext["preamble"]   = FORTRAN_MODULE_PREAMBLE
    fContext["used"]       = ["hipfort","hipfort_check"]
    fContext["interfaces"] = []
    fContext["routines"]   = []

    # extract kernels
    loopKernels      = stree.findAll(filter=lambda child: isinstance(child, scanner.STLoopKernel) and select(child), recursively=True)
    deviceProcedures = stree.findAll(filter=lambda child: type(child) is scanner.STProcedure and child.mustBeAvailableOnDevice() and select(child), recursively=True)

    if (len(loopKernels) or len(deviceProcedures)):
        updateContextFromLoopKernels(loopKernels,index,hipContext,fContext)
        updateContextFromDeviceProcedures(deviceProcedures,index,hipContext,fContext)
        if generateCode:
            renderTemplates(outputFilePrefix,hipContext,fContext)

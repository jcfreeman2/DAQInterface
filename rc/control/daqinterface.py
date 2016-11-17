#!/bin/env python

import sys
sys.path.append("/home/nfs/dunedaq/jcfree/standalone_daq")

import argparse
import datetime
import os
import subprocess
from subprocess import Popen
from time import sleep, time
import sys
import traceback
import re
import random
import string
import glob
import stat
from threading import Thread

#from rc.io import sender
from rc.io.timeoutclient import TimeoutServerProxy
from rc.util import wait_for_interrupt
from rc.control.component import Component # , announcing_sender
from rc.compatibility import xmlrpclib
from rc.control.deepsuppression import deepsuppression

from rc.control.get_config_info_protodune import get_config_info_base
from rc.control.put_config_info_protodune import put_config_info_base
from rc.control.save_run_record_35ton import save_run_record_base

class DAQInterface(Component):
    """
    DAQInterface: The intermediary between Run Control, the
    configuration database, and artdaq processes

    """

    # "Procinfo" is basically just a simple structure containing all
    # the info about a given artdaq process that DAQInterface might
    # care about

    # However, it also contains a less-than function which allows it
    # to be sorted s.t. processes you'd want shutdown first appear
    # before processes you'd want shutdown last (in order:
    # boardreader, eventbuilder, aggregator)

    # JCF, Nov-17-2015

    # I add the "fhicl_file_path" variable, which is a sequence of
    # paths which are searched in order to cut-and-paste #include'd
    # files (see also the description of the DAQInterface class's
    # fhicl_file_path variable, whose sole purpose is to be passed to
    # Procinfo's functions)

    class Procinfo(object):
        def __init__(self, name="", host="", port="-999", fhicl="", fhicl_file_path = []):
            self.name = name
            self.port = port
            self.host = host
            self.fhicl = fhicl     # Name of the input FHiCL document
            self.ffp = fhicl_file_path

            # FHiCL code actually sent to the process

            # JCF, 11/11/14 -- note that "fhicl_used" will be modified
            # during the initalization function, as bookkeeping, etc.,
            # is performed on FHiCL parameters

            if self.fhicl is not None:
                self.fhicl_used = ""
                self.recursive_include(self.fhicl)
            else:
                self.fhicl_used = None

            # JCF, Jan-14-2016

            # Do NOT change the "lastreturned" string below without
            # changing the commensurate string in check_proc_errors!

            self.lastreturned = "DAQInterface: ARTDAQ PROCESS NOT YET CALLED"
            self.socketstring = "http://" + self.host + ":" + self.port \
                + "/RPC2"

        def update_fhicl(self, fhicl):
            self.fhicl = fhicl
            self.fhicl_used = ""
            self.recursive_include(self.fhicl)

        def __lt__(self, other):
            if self.name != other.name:
                if self.name == "BoardReader":
                    return True
                elif self.name == "EventBuilder":
                    if other.name == "Aggregator":
                        return True
                return False
            else:
                if int(self.port) < int(other.port):
                    return True
                return False

        def recursive_include(self, filename):
            if self.fhicl is not None:            
                for line in open(filename).readlines():

                    if "#include" not in line:
                        self.fhicl_used += line
                    else:
                        res = re.search(r"#include\s+\"(\S+)\"", line)
                        
                        if not res:
                            raise Exception("Error in Procinfo::recursive_include: "
                                            "unable to parse line \"%s\" in %s" %
                                            (line, filename))

                        included_file = res.group(1)

                        if included_file[0] == "/":
                            if not os.path.exists(included_file):
                                raise Exception("Error in Procinfo::recursive_include: "
                                                "unable to find file %s" %
                                                included_file)
                            else:
                                self.recursive_include(included_file)
                        else:
                            found_file = False
                            
                            for dirname in self.ffp:
                                if os.path.exists( dirname + "/" + included_file) and not found_file:
                                    self.recursive_include(dirname + "/" + included_file)
                                    found_file = True

                            if not found_file:
                                
                                ffp_string = ":".join(self.ffp)

                                raise Exception("Error in Procinfo::recursive_include: "
                                                "unable to find file %s in list of "
                                                "the following fhicl_file_paths: %s" %
                                                (included_file, ffp_string))
                            
    def date_and_time(self):
        return Popen("date", shell=True, stdout=subprocess.PIPE).stdout.readlines()[0].strip()

    def print_log(self, printstr, debuglevel=-999):
        self.logger.log(printstr)

        if self.debug_level >= debuglevel:
            print "%s: %s" % (self.date_and_time(), printstr)

    # JCF, 3/11/15

    # "get_pids" is a simple utility function which will go to the
    # requested host (defaults to the local host), and searches for a
    # process by grep-ing for the passed greptoken in the process
    # table returned by "ps aux". It returns a (possibly empty) list
    # of the process IDs found

    def get_pids(self, greptoken, host="localhost"):

        cmd = 'ps aux | grep "%s" | grep -v grep' % (greptoken)

        if host != "localhost":
            cmd = "ssh -f " + host + " '" + cmd + "'"

        proc = Popen(cmd, shell=True, stdout=subprocess.PIPE)

        lines = proc.stdout.readlines()

        pids = [line.split()[1] for line in lines]

        return pids

    # JCF, 1/1/15

    # Basically, reset (and, if this is its first call, initialize)
    # all DAQInterface configuration variables to their default
    # values; this should be called both in the constructor as well as
    # in do_initialize()

    def reset_DAQInterface_config(self):

        # The build directory in which the lbne-artdaq package to be
        # used is located
        self.lbneartdaq_build_dir = None

        # The host on which artdaq's pmt.rb artdaq-process control
        # script will run
        self.pmt_host = None

        # And its port

        self.pmt_port = None

        # The pause, in seconds, after firing up the artdaq processes but
        # before issuing the init transition to them
        self.pause_before_initialization = None

        # A self.debug_level of 0 means minimal diagnostic output;
        # higher values mean increasing diagnostic output

        self.debug_level = 999

        # The directory to which artdaq output gets logged
        self.log_directory = None

        # The directory to which the FHiCL code used to initialize the
        # artdaq processes is saved for posterity
        self.record_directory = None

        # This variable determines whether or not we'll require that
        self.disable_configuration_check = None

        # If "tdu_xmlrpc_port" is set to a number greater than 0, the
        # attempt_sync_pulse() function, called at the beginning of
        # do_start_running() and do_resume_running(), will send a sync
        # pulse to the TDU with "tdu_xmlrpc_port" being used as the
        # port number for the XML-RPC server linked to the TDU

        self.tdu_xmlrpc_port = None

        # "procinfos" will be an array of Procinfo structures (defined
        # below), where Procinfo contains all the info DAQInterface needs
        # to know about an individual artdaq process: name, host, port,
        # and FHiCL initialization document. Filled through a combination
        # of info in the DAQInterface configuration file as well as Jon
        # Paley's configuration manager

        self.procinfos = []

    # Constructor for DAQInterface begins here

    def __init__(self, logpath=None, name="toycomponent",
                 rpc_host="localhost", control_host='localhost',
                 synchronous=True, rpc_port=6659,
                 config_filename=""):

        # Initialize Component, the base class of DAQInterface

        Component.__init__(self, logpath=logpath,
                           name=name,
                           rpc_host=rpc_host,
                           control_host=control_host,
                           synchronous=synchronous,
                           rpc_port=rpc_port,
                           skip_init=False)

        # JCF, Aug-28-2015
        # Piece together the call to msgviewer...

        cmds = []
        #cmds.append(". /data/lbnedaq/products/setup")
        cmds.append(". /home/nfs/products/setup")
        cmds.append("setup artdaq_mfextensions v1_01_00 -q prof:e10:s35")
        cmds.append("msgviewer -c $ARTDAQ_MFEXTENSIONS_FQ_DIR/bin/msgviewer.fcl 2>&1 > /dev/null &" )

        msgviewercmd = " ; ".join(cmds)

        with deepsuppression():
            status = Popen(msgviewercmd, shell=True).wait()

        if status != 0:
            raise Exception("Exception in DAQInterface: " +
                            "status error raised in msgviewer call within Popen")

        # JCF, Nov-17-2015

        # fhicl_file_path is a sequence of directory names which will
        # be searched for any FHiCL documents #include'd by the main
        # document used to initialize each artdaq process, but not
        # given with an absolute path in the #include .

        self.fhicl_file_path = []

        # JCF, Nov-7-2015

        # Now that we're going with a multithreaded (simultaneous)
        # approach to sending transition commands to artdaq processes,
        # when an exception is thrown a thread the main thread needs
        # to know about it somehow - thus this new exception variable

        self.exception = False

        # JCF, 11/18/14

        # "config_filename" refers to the file users can edit to
        # configure DAQInterface, as well as which systems various
        # artdaq processes will run on + the FHiCL documents which
        # will initalize them. For details, see
        # https://cdcvs.fnal.gov/redmine/projects/lbne-daq/wiki/Running_DAQ_Interface

        if config_filename != "":
            self.config_filename = config_filename
        else:
            self.config_filename = "/".join(__file__.split("/")[:-3]) + \
                "/docs/config.txt"

        # JCF, 11/13/14

        # This is the # of CfgMgrApp instances running; this determines
        # where FHiCL documents are searched for and what constitutes
        # illegal config.txt formatting

        self.num_config_procs = None

        # This will contain the directory which Jon Paley's CfgMgrApp
        # will use to supply DAQInterface with FHiCL documents

        self.config_dirname = None

        # This keeps a record of the last line presented by the
        # display_lbne_artdaq_output() function, so it isn't
        # repeatedly printed to screen

        self.last_lbne_artdaq_line = None

        # See definition, above
        self.reset_DAQInterface_config()

        self.__do_initialize = False
        self.__do_start_running = False
        self.__do_stop_running = False
        self.__do_terminate = False
        self.__do_pause_running = False
        self.__do_resume_running = False
        self.__do_recover = False

        self.messagefacility_fhicl = "/home/nfs/dunedaq/daqarea/lbnerc/docs/MessageFacility.fcl"

        if self.debug_level >= 1:
            print "DAQInterface launched; if running DAQInterface in the background," \
                " can press <enter> to return to shell prompt"

    get_config_info = get_config_info_base
    put_config_info = put_config_info_base
    save_run_record = save_run_record_base


    # The actual transition functions called by Run Control; note
    # these just set booleans which are tested in the runner()
    # function, called periodically by run control

    def initialize(self):
        self.__do_initialize = True

    def start_running(self):
        self.__do_start_running = True

    def stop_running(self):
        self.__do_stop_running = True

    def terminate(self):
        self.__do_terminate = True

    def pause_running(self):
        self.__do_pause_running = True

    def resume_running(self):
        self.__do_resume_running = True

    def recover(self):
        self.__do_recover = True

    # JCF, 6/26/14

    # Send a simple acknowledgement to RC that something went wrong

    # JCF, 8/11/14 -- add capability to provide extra information

    # JCF, 11/18/14 -- and bind the recover transition to sending RC
    # an alert

    def alert_and_recover(self, extrainfo=None):

        #errmsg = "%s: Error in DAQInterface" % (self.date_and_time())

        #if extrainfo:
        #    errmsg += ": " + extrainfo

        #trep = datetime.datetime.utcnow()
        # self.sender.send({"type": "alert",
        #                   "service": self.name,
        #                   "t": trep,
        #                   "varname": "errmsg",
        #                   "value": errmsg})

        self.recover()
        return

    def check_proc_errors(self):

        is_all_ok = True

        # JCF, Jan-14-2016

        # The "5" below is fairly ad-hoc; I'll want to play around
        # with it to find the value that makes the most sense in an
        # operational context

        # JCF, Jan-17-2016

        # To clarify what's happening: now, if a process doesn't
        # return "Success", as long as the response is one of the
        # following shown below ("ARTDAQ PROCESS NOT YET CALLED", or a
        # legitimate DAQ state) and not an obvious error message, for
        # max_retries times the program will sleep for a second and
        # then query the process again, the idea being that the lack
        # of a "Success" response was due to the
        # "procinfo.lastreturned" variable simply not getting filled
        # in a timely manner

        for procinfo in self.procinfos:

            if procinfo.lastreturned != "Success":

                redeemed=False
                max_retries=20
                retry_counter=0
                
                while retry_counter < max_retries and ( 
                    "ARTDAQ PROCESS NOT YET CALLED" in procinfo.lastreturned or
                    "Stopped" in procinfo.lastreturned or
                    "Ready" in procinfo.lastreturned or
                    "Running" in procinfo.lastreturned or
                    "Paused" in procinfo.lastreturned ):
                    retry_counter += 1
                    sleep(1)
                    if procinfo.lastreturned  == "Success":
                        redeemed=True

                if redeemed:
                    successmsg = "After " + str(retry_counter) + " checks, process " + \
                        procinfo.name + " at " + procinfo.host + ":" + procinfo.port + " returned \"Success\""
                    self.print_log( successmsg )
                    continue  # We're fine, continue on to the next process check

                errmsg = "process " + procinfo.name + " at " + procinfo.host + \
                    ":" + procinfo.port + " returned the following: \"" + \
                    procinfo.lastreturned + "\""
                self.print_log("Error in DAQInterface: ")
                self.print_log(errmsg)

                is_all_ok = False

        if not is_all_ok:
            self.alert_and_recover("At least one artdaq process "
                                   "failed a transition")
            return


    # Utility functions used to count the different process types

    def num_boardreaders(self):
        num_boardreaders = 0
        for procinfo in self.procinfos:
            if "BoardReader" in procinfo.name:
                num_boardreaders += 1
        return num_boardreaders

    def num_eventbuilders(self):
        num_eventbuilders = 0
        for procinfo in self.procinfos:
            if "EventBuilder" in procinfo.name:
                num_eventbuilders += 1
        return num_eventbuilders

    def num_aggregators(self):
        num_aggregators = 0
        for procinfo in self.procinfos:
            if "Aggregator" in procinfo.name:
                num_aggregators += 1
        return num_aggregators

    # JCF, 8/11/14

    # launch_procs() will create the artdaq processes

    def launch_procs(self):

        greptoken = "pmt.rb -p " + self.pmt_port
        pids = self.get_pids(greptoken, self.pmt_host)

        if len(pids) != 0:
            raise Exception("Exception in DAQInterface: "
                            "\"pmt.rb -p %s\" was already running on %s" %
                            (self.pmt_port, self.pmt_host))

        if not os.path.isdir(self.lbneartdaq_build_dir + "/lbne-artdaq"):
            print "Unable to find " + self.lbneartdaq_build_dir + \
                "/lbne-artdaq; have you run \"source_me\"?"
            raise Exception("Exception in DAQInterface: " +
                            "lbne-artdaq not found in " +
                            self.lbneartdaq_build_dir)

        if self.debug_level > 1:

            print "DAQInterface: Based on procinfos array, will launch " + \
                str(self.num_boardreaders()) + \
                " BoardReaderMain processes, " + \
                str(self.num_eventbuilders()) + \
                " EventBuilderMain processes, and " + \
                str(self.num_aggregators()) + \
                " AggregatorMain processes"

            print "Assuming lbne-artdaq directory is " + \
                self.lbneartdaq_build_dir

        # We'll use the desired features of the artdaq processes to
        # create a text file which will be passed to artdaq's pmt.rb
        # program

        pmtconfigname = "/tmp/pmtConfig." + \
            ''.join(random.choice(string.digits)
                    for _ in range(5))

        outf = open(pmtconfigname, "w")

        if not outf:
            raise Exception("Exception in DAQInterface: " +
                            "unable to open temporary file " +
                            pmtconfigname)

        for procinfo in self.procinfos:

            for progname in ["BoardReader", "EventBuilder", "Aggregator"]:
                if progname in procinfo.name:
                    outf.write(progname + "Main ")

            outf.write(procinfo.host + " " + procinfo.port + "\n")

        outf.close()

        if self.pmt_host != "localhost":
            status = Popen("scp -p " + pmtconfigname + " " +
                           self.pmt_host + ":/tmp", shell=True).wait()

            if status != 0:
                raise Exception("Exception in DAQInterface: unable to copy " +
                                pmtconfigname + " to " + self.pmt_host)

        cmds = []

        # Following if-else is Python-ese for "directory a level up
        # from the lbne-artdaq build directory", i.e., where we expect
        # to find the setupLBNEARTDAQ script

        if self.lbneartdaq_build_dir[-1] == "/":
            setupdir = "/".join(self.lbneartdaq_build_dir.split("/")[0:-2])
        else:
            setupdir = "/".join(self.lbneartdaq_build_dir.split("/")[0:-1])

        if not os.path.exists(setupdir + "/setupLBNEARTDAQ"):
            raise Exception("Exception in DAQInterface: " +
                            "setupLBNEARTDAQ script not found in " +
                            setupdir)

        for logdir in ["pmt", "masterControl", "boardreader", "eventbuilder",
                       "aggregator"]:
            cmds.append("mkdir -p -m 0777 " + self.log_directory +
                        "/" + logdir)

        cmds.append("cd " + self.lbneartdaq_build_dir)
        cmds.append("source " + setupdir + "/setupLBNEARTDAQ " +
                    self.lbneartdaq_build_dir)
        cmds.append("source " + self.lbneartdaq_build_dir +
                    "/bin/setupDemoEnvironment.sh")

        cmds.append("export LBNEARTDAQ_PMT_PORT=" + self.pmt_port)

        # This needs to use the same messagefacility package version
        # as lbne-artdaq ultimately relies on, otherwise things will
        # fail

        cmds.append("setup artdaq_mfextensions v1_01_00 -q prof:e10:s35")

        cmds.append("export ARTDAQ_PROCESS_FAILURE_EXIT_DELAY=30")

        cmd = "pmt.rb -p $LBNEARTDAQ_PMT_PORT -d " + pmtconfigname + \
            " --logpath " + self.log_directory + \
            " --logfhicl " + self.messagefacility_fhicl + " --display $DISPLAY & "
   
        cmds.append(cmd)

        launchcmd = " ; ".join(cmds)

        if self.pmt_host != "localhost":
            launchcmd = "ssh -f " + self.pmt_host + " '" + launchcmd + "'"

        if self.debug_level >= 3:
            print "PROCESS LAUNCH COMMANDS: "
            print launchcmd
            print

        status = 1

        if self.debug_level >= 2:
            status = Popen(launchcmd, shell=True).wait()
        else:
            with deepsuppression():
                status = Popen(launchcmd, shell=True).wait()

        if status != 0:
            raise Exception("Exception in DAQInterface: " +
                            "status error raised in pmt.rb call within Popen")

    # check_proc_heartbeats() will check that the expected artdaq
    # processes are up and running

    def check_proc_heartbeats(self, requireSuccess=True):

        is_all_ok = True

        for procinfo in self.procinfos:

            proctype = ""
            if "BoardReader" in procinfo.name:
                proctype = "BoardReaderMain"
            elif "EventBuilder" in procinfo.name:
                proctype = "EventBuilderMain"
            elif "Aggregator" in procinfo.name:
                proctype = "AggregatorMain"
            else:
                self.alert_and_recover("Exception in DAQInterface:"
                                       " unknown process type found"
                                       " in procinfos.keys()")

            greptoken = proctype + " -p " + procinfo.port

            pids = self.get_pids(greptoken, procinfo.host)

            num_procs_found = len(pids)

            if num_procs_found != 1:
                is_all_ok = False

                if requireSuccess:
                    errmsg = "process " + procinfo.name + \
                        " at " + procinfo.host + ":" + \
                        procinfo.port + " not found"
                    self.print_log("Error in "
                                   "DAQInterface::check_proc_heartbeats(): "
                                   "please check messageviewer and/or the logfiles for error messages")
                    self.print_log(errmsg)

            else:
                if self.debug_level >= 4:
                    print "Just checked for " + token + ", looks OK"

        if not is_all_ok and requireSuccess:
            self.alert_and_recover("Heartbeat failure "
                                   "of at least one artdaq process; please check messageviewer and/or the logfiles for error messages")
            return is_all_ok

        return is_all_ok

    # JCF, 5/29/15

    # check_proc_exceptions() takes advantage of an artdaq feature
    # developed by Kurt earlier this month whereby if something goes
    # wrong in an artdaq process during running (e.g., a fragment
    # generator's getNext_() function throws an exception) then, when
    # queried, the artdaq process can return an "Error" state, as
    # opposed to the usual DAQ states ("Ready", "Running", etc.)

    def check_proc_exceptions(self):

        is_all_ok = True

        for procinfo in self.procinfos:

            try:
                procinfo.lastreturned = procinfo.server.daq.status()
            except Exception:
                self.exception = True
                exceptstring = "Exception caught in DAQInterface attempt to query status of artdaq process %s at %s:%s ; most likely reason is process no longer exists" % \
                    (procinfo.name, procinfo.host, procinfo.port)              
                self.print_log(exceptstring)

            if procinfo.lastreturned == "Error":
                is_all_ok = False
                errmsg = "\"Error\" state returned by process %s at %s:%s; please check messageviewer and/or the logfiles for error messages" % \
                    (procinfo.name, procinfo.host, procinfo.port)

                self.print_log(errmsg)

        if not is_all_ok:
            self.alert_and_recover("One or more artdaq processes"
                                   " discovered to be in \"Error\" state")

    # JCF, 1/28/15

    # The idea behind the "display_lbne_artdaq_output()" function is
    # that in the runner() function, after checking that all artdaq
    # processes are alive, the PMT logfile is then examined for
    # red-flag terms like "exception" and "error", and any lines with
    # these terms get displayed

    def display_lbne_artdaq_output(self):

        keywords = ["error", "exception", "back-pressure", "MSG-e", "errno"]

        grepstring = "\|".join(keywords)

        cmds = []

        cmds.append("cd %s/pmt" % (self.log_directory))
        cmds.append("most_recent_logfile=$(ls -tr1 %s )" %
                    (self.log_filename_wildcard))

        # JCF, 5/1/15

        # Want to avoid accidentally grepping an old pmt logfile which
        # happens to share the same process ID as the current one

        cmds.append("if [[ $(find \"$most_recent_logfile\" -mmin -60) ]];"
                    " then grep -i1 \"%s\" $most_recent_logfile; fi"
                    % (grepstring))

        cmd = ";".join(cmds)

        if self.pmt_host != "localhost":
            cmd = "ssh -f " + self.pmt_host + " '" + cmd + "'"

        status = Popen(cmd, shell=True, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)

        lines = status.stdout.readlines()

        if len(lines) > 1:

            # "1" because we're using the "-1" option to grep
            line = lines[1].strip()

            if line != self.last_lbne_artdaq_line:
                self.last_lbne_artdaq_line = line

                for tmpline in lines:
                    print tmpline.strip()

    # JCF, Nov-9-2015

    def attempt_lcm_pulse(self, start_or_stop):

        conf_file = "%s/%s/lcm_%s.conf" % (self.config_dirname,
                                          self.run_params["config"],
                                          start_or_stop)
                                         
        if not os.path.exists(conf_file):
            raise Exception("Error in DAQInterface::attempt_lcm_pulse : " + 
                            "unknown lcm configuration file \"%s\"" % (conf_file))

        cmds = []

        # See launch_procs() for info on this string jujitsu

        if self.lbneartdaq_build_dir[-1] == "/":
            setupdir = "/".join(self.lbneartdaq_build_dir.split("/")[0:-2])
        else:
            setupdir = "/".join(self.lbneartdaq_build_dir.split("/")[0:-1])

        cmds.append("cd " + self.lbneartdaq_build_dir)
        cmds.append("source " + setupdir + "/setupLBNEARTDAQ " +
                    self.lbneartdaq_build_dir)


#        cmds.append("cd /data/lbnedaq/lcmControl")
#        cmds.append("source setup.sh")
#        cmds.append("./bin/lcmControl.exe %s" % (conf_file))

#        if self.debug_level >= 1:
#            print
#            print "About to call lcmControl.exe ; no warning message below this will imply success..."

#        with deepsuppression():
#            status = Popen("ssh lbnedaq1 ' " + ";".join(cmds) + " ' ", shell=True).wait()

#        if status != 0:
#            self.print_log("Warning in DAQInterface::attempt_lcm_pulse : " +
#                           "error status code returned by lcmControl.exe call")
        
        print

        return

    # JCF, 2/6/15

    # attempt_sync_pulse() will send a sync pulse to the TDU via an
    # XML-RPC server connection assuming the port of the XML-RPC
    # server to the TDU is set to a non-negative integer. For more on
    # the TDU and its code, take a look at Tom Dealtry's notes at
    # https://cdcvs.fnal.gov/redmine/projects/lbne-daq/wiki/Starting_and_using_TDUControl

    def attempt_sync_pulse(self):

        if int(self.tdu_xmlrpc_port) <= 0:
            self.print_log("XML-RPC server port for the TDU is set to <= 0;" +
                           " skipping sync pulse")
            return

        if os.getcwd().split("/")[-1] != "lbnerc":
            raise Exception("Exception in DAQInterface: " +
                            "expected to be in lbnerc/ directory")

        # JCF, 2/7/15 -- should I add a "ping" before sending the sync pulse?

        tdu_attempts = 5

        # JCF, Jan-25-2016

        # Due to the reconfiguration of the network this morning
        # performed by Geoff Savage, Tim Nicholls and Bonnie King, I'm
        # now launching the tdu_control_via_xmlrpcserver.py script
        # remotely on lbnedaq1

        cmd = "ssh lbnedaq1 'cd /data/lbnedaq/daqarea ; . fireup ; " + \
            "python rc/tdu/testing_scripts/tdu_control_via_xmlrpcserver.py " + \
            "-T 10.226.8.18 -p %s -s'" % (self.tdu_xmlrpc_port)

        for tdu_attempt in range(tdu_attempts):
            result = Popen(cmd, shell=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)

            lines = result.stdout.readlines()

            if lines[-1].strip() == "0 (SUCCESS)":
                print "TDU RESULT: " + lines[-1]
                break
            else:
                for line in lines:
                    print line

        if not (lines[-1].strip() == "0 (SUCCESS)"):
             raise Exception("Exception in DAQInterface:" +
                             " problem running the" +
                             " tdu_control_via_xmlrpcserver.py script")

    def kill_procs(self):

        # JCF, 12/29/14

        # If the PMT host hasn't been defined, we can be sure there
        # aren't yet any artdaq processes running yet (or at least, we
        # won't be able to determine where they're running!)

        if self.pmt_host is None:
            return

        # Now, the commands which will clean up the pmt.rb + its child
        # artdaq processes

        pmt_pids = self.get_pids("pmt.rb -p " + str(self.pmt_port),
                                 self.pmt_host)

        if len(pmt_pids) > 0:

            cmd = "kill %s" % (pmt_pids[0])

            if self.pmt_host != "localhost":
                cmd = "ssh -f " + self.pmt_host + " '" + cmd + "'"

            status = Popen(cmd, shell=True, stdout=subprocess.PIPE)

        for procinfo in self.procinfos:
            greptoken = procinfo.name + "Main -p " + procinfo.port

            pids = self.get_pids(greptoken, procinfo.host)

            if len(pids) > 0:
                cmd = "kill -9 " + pids[0]

                if procinfo.host != "localhost":
                    cmd = "ssh -f " + procinfo.host + " '" + cmd + "'"

                Popen(cmd, shell=True, stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT)

                # Check that it was actually killed

                sleep(1)

                pids = self.get_pids(greptoken, procinfo.host)

                if len(pids) > 0:
                    self.print_log("Error in "
                                   "DAQInterface::kill_procs(): ")
                    self.print_log("Appeared to be unable to kill \"%s\""
                                   " on %s" % greptoken, procinfo.host)

        self.procinfos = []

        return

    # JCF, 12/2/14

    # Given the directory name of a git repository, this will either
    # return the most recent hash commit in the repo or, if a problem
    # occurred, the "None" value

    # If this function returns "None", your next action should be to
    # return from the caller, so that the recover transition can occur
    # immediately

    def get_commit_hash(self, gitrepo, requireSuccess=True):
        cmds = []
        cmds.append("cd %s" % (gitrepo))
        cmds.append("git log | head -1 | awk '{print $2}'")

        proc = Popen(";".join(cmds), shell=True,
                     stdout=subprocess.PIPE)
        proclines = proc.stdout.readlines()

        if len(proclines) != 1 or len(proclines[0].strip()) != 40:
            if requireSuccess:
                self.alert_and_recover("Commit hash for %s not found" % (gitrepo))
                return None
            else:
                print "WARNING: directory " + gitrepo + " does not appear to be a git repository; " + \
                    "no commit hash to record"
                return "Not a git repository"

        return proclines[0].strip()

    # The function to read in the DAQInterface configuration file

    def read_DAQInterface_config(self):
        inf = open(self.config_filename)

        if not inf:
            raise Exception("Exception in DAQInterface: " +
                            "unable to locate configuration file \"" +
                            self.config_filename + "\"")

        memberDict = {"name": None, "host": None, "port": None, "fhicl": None}

        for line in inf.readlines():

            # Is this line a comment?
            res = re.search(r"\s*#", line)
            if res:
                continue

            res = re.search(r"\s*PMT host\s*:\s*(\S+)", line)
            if res:
                self.pmt_host = res.group(1)
                continue

            res = re.search(r"\s*PMT port\s*:\s*(\S+)", line)
            if res:
                self.pmt_port = res.group(1)
                continue

            res = re.search(r"\s*lbne-artdaq\s*:\s*(\S+)",
                            line)
            if res:
                self.lbneartdaq_build_dir = res.group(1)
                continue

            res = re.search(r"\s*log directory\s*:\s*(\S+)",
                            line)
            if res:
                self.log_directory = res.group(1)
                continue

            res = re.search(r"\s*record directory\s*:\s*(\S+)",
                            line)
            if res:
                self.record_directory = res.group(1)
                continue

            res = re.search(r"\s*TDU XMLRPC port\s*:\s*(\S+)",
                            line)
            if res:
                self.tdu_xmlrpc_port = res.group(1)
                continue

            res = re.search(r"\s*disable configuration check\s*:\s*(\S+)",
                            line)
            if res:
                inputstring = res.group(1)
                if inputstring.lower() == "true":
                    self.disable_configuration_check = True
                elif inputstring.lower() == "false":
                    self.disable_configuration_check = False
                else:
                    raise Exception("Exception in DAQInterface: "
                                    "problem parsing " + self.config_filename)
                continue

            res = re.search(r"\s*debug level\s*:\s*(\S+)",
                            line)
            if res:
                self.debug_level = int(res.group(1))
                continue

            res = re.search(r"\s*pause before initialization\s*:\s*(\S+)",
                            line)
            if res:
                self.pause_before_initialization = int(res.group(1))
                continue

            if "EventBuilder" in line or "Aggregator" in line:

                res = re.search(r"\s*(\w+)\s+(\S+)\s*:\s*(\S+)", line)

                if not res:
                    raise Exception("Exception in DAQInterface: "
                                    "problem parsing " + self.config_filename)

                memberDict["name"] = res.group(1)
                memberDict[res.group(2)] = res.group(3)

                # Has the dictionary been filled s.t. we can use it to
                # initalize a procinfo object?

                # JCF, 11/13/14

                # Note that if the configuration manager is running,
                # then we expect the AggregatorMain applications to
                # have a host and port specified in config.txt, but
                # not a FHiCL document

                # JCF, 3/19/15

                # Now, we also expect only a host and port for
                # EventBuilderMain applications as well

                filled = True

                for label, value in memberDict.items():
                    if value is None and not label == "fhicl":
                        filled = False

                # If it has been filled, then initialize a Procinfo
                # object, append it to procinfos, and reset the
                # dictionary values to null strings

                if filled:
                    self.procinfos.append(self.Procinfo(memberDict["name"],
                                                        memberDict["host"],
                                                        memberDict["port"],
                                                        memberDict["fhicl"],
                                                        self.fhicl_file_path))
                    for varname in memberDict.keys():
                        memberDict[varname] = None

        # Check that the configuration file actually contained the
        # definitions we wanted

        # The BoardReaderMain info should be supplied by the
        # configuration manager; info for both AggregatorMains and the
        # EventBuilderMains (excluding their FHiCL documents) should
        # be supplied in the DAQInterface configuration file

        if self.num_boardreaders() != 0 or \
                self.num_eventbuilders() == 0:
            errmsg = "Unexpected number of artdaq processes provided " \
                "by the DAQInterface config file: " \
                "%d BoardReaderMains, %d EventBuilderMains " \
                "(expect 0 BoardReaderMains, >0 EventBuilderMains)" % \
                (self.num_boardreaders(),
                 self.num_eventbuilders(),
                 self.num_aggregators())

            raise Exception(errmsg)

        undefined_var = ""

        if self.pmt_host is None:
            undefined_var = "PMT host"
        elif self.lbneartdaq_build_dir is None:
            undefined_var = "lbne-artdaq"
        elif self.log_directory is None:
            undefined_var = "log directory"
        elif self.record_directory is None:
            undefined_var = "record directory"
        elif self.tdu_xmlrpc_port is None:
            undefined_var = "TDU XMLRPC port"
        elif self.disable_configuration_check is None:
            undefined_var = "disable configuration check"
        elif self.debug_level is None:
            undefined_var = "debug level"
        elif self.pause_before_initialization is None:
            undefined_var = "pause before initialization"

        if undefined_var != "":
            errmsg = "Error: \"%s\" undefined in " \
                "DAQInterface config file" % \
                (undefined_var)
            raise Exception(errmsg)

    # JCF, 3/17/15

    # Define the local function "get_logfilenames()" which will
    # enable us not just to get the pmt*.log logfile, but also the
    # artdaq-process-specific logfiles as well

    def get_logfilenames(self, subdir, nfiles):

        cmd = "ls -tr1 %s/%s | tail -%d" % (self.log_directory,
                                            subdir, nfiles)

        if self.pmt_host != "localhost":
            cmd = "ssh -f " + self.pmt_host + " '" + cmd + "'"

        proc = Popen(cmd, shell=True, stdout=subprocess.PIPE)
        proclines = proc.stdout.readlines()

        if len(proclines) != nfiles:
            raise Exception("Exception in DAQInterface: " +
                            "problem seeking logfile(s)")

        logfilenames = []

        for line in proclines:
            logfilenames.append(line.strip())

        return logfilenames

    # JCF, Aug-12-2016

    # get_run_documents is intended to be called after the FHiCL
    # documents have been fully formatted and are ready to send to the
    # artdaq processes; essentially, this just creates a big string
    # which FHiCL can parse but doesn't actually use, and which
    # contains all the FHiCL documents used for all processes, as well
    # as other information pertinent to the run (its metadata output
    # file, etc.). This string is intended to be concatenated at the
    # end of the diskwriting aggregator FHiCL document(s) so that any
    # output file from the DAQ will have a full record of how the DAQ
    # was configured when the file was created when "config_dumper -P
    # <rootfile>" is run

    def get_run_documents(self):

        runstring = "\n\nrun_documents: {\n"
        
        boardreader_cntr = 0
        eventbuilder_cntr = 0
        aggregator_cntr = 0

        for procinfo in self.procinfos:
            if "BoardReader" in procinfo.name:
                boardreader_cntr += 1
                runstring += "\n\n  BOARDREADER_" + procinfo.host.replace(".","_") + "_" + str(procinfo.port) + ": '\n"
            elif "EventBuilder" in procinfo.name:
                eventbuilder_cntr += 1
                runstring += "\n\n  EVENTBUILDER_" + procinfo.host.replace(".","_") + "_" + str(procinfo.port) + ": '\n"
            elif "Aggregator" in procinfo.name:
                aggregator_cntr += 1
                runstring += "\n\n  AGGREGATOR_" + procinfo.host.replace(".","_") + "_" + str(procinfo.port) + ": '\n"
            else:
                self.alert_and_recover("Exception in DAQInterface:"
                                       " unknown process type found"
                                       " in procinfos.keys()")

            dressed_fhicl = re.sub("'","\\'", procinfo.fhicl_used)
            runstring += dressed_fhicl
            runstring += "\n  '\n"
        
        def get_arbitrary_file(filename, label):
            try:
                file = open(filename)
            except:
                self.alert_and_recover("Exception in DAQInterface: unable to find file \"%s\"" % 
                                       (filename))
                return "999"

            contents = "\n  " + label + ": '\n"

            for line in file:
                line = re.sub("'","\\'", line)
                contents += line

            contents += "\n  '\n"
            return contents

        indir = self.record_directory + "/" + str(self.run_params["run_number"])

        metadata_filename = indir + "/metadata_r" + str(self.run_params["run_number"]) + ".txt"
        runstring += get_arbitrary_file(metadata_filename, "run_metadata")

        config_filename = indir + "/config_r" + str(self.run_params["run_number"]) + ".txt"        
        runstring += get_arbitrary_file(config_filename, "run_daqinterface_config")


        has_rces = False

        for component in self.run_params["daq_comp_list"] :
            if "rce" in component:
                has_rces = True

        if has_rces:
            rce_xml_filename = indir + "/defaults.xml"
            runstring += get_arbitrary_file(rce_xml_filename, "run_rce_xml")

        runstring += "} \n\n"

        return runstring

    # JCF, Nov-8-2015

    # The core functionality for "do_command" is that it will launch a
    # separate thread for each transition issued to an individual
    # artdaq process; for init, start, and resume it will send the
    # command simultaneously to the aggregators, wait for the threads
    # to join, and then do the same thing for the eventbuilders and
    # then the boardreaders. For stop and pause, it will do this in
    # reverse order of upstream/downstream.

    # Note that since "initialize", "start" and "stop" all require
    # additional actions besides simply sending transitions to
    # processes and waiting for their response, "do_command" is not
    # meant to be a replacement for "do_initialize",
    # "do_start_running" and "do_stop_running" the way it IS meant to
    # be a replacement for "do_pause_running", etc., but rather, is
    # meant to be called in the body of those functions. Thus, for
    # those transitions, some functionality (e.g., announding the
    # transition is underway at the beginning of the function, and
    # calling "complete_state_change" at the end) is not applied.

    def do_command(self, command):

        if command != "Start" and command != "Init" and command != "Stop":
            if self.debug_level >= 1:
                print "%s: DAQInterface: \"%s\" transition underway" % \
                    (self.date_and_time(), command)

        # "process_command" is the function which will send a
        # transition to a single artdaq process, and be run on its own
        # thread so that transitions to different processes can be
        # sent simultaneously
                
        # Note that since Python is "pass-by-object-reference" (see
        # http://robertheaton.com/2014/02/09/pythons-pass-by-object-reference-as-explained-by-philip-k-dick/
        # for more), I pass it the index of the procinfo struct we
        # want, rather than the actual procinfo struct

        def process_command(self, procinfo_index, command):

            try:

                if command == "Init":
#                    if not "Aggregator" in self.procinfos[procinfo_index].name:
                    if True:
                        self.procinfos[procinfo_index].lastreturned = \
                            self.procinfos[procinfo_index].server.daq.init(self.procinfos[procinfo_index].fhicl_used)
                    else:
                        print "JCF: THIS CODE STILL NEEDS TO BE DEBUGGED"
                        self.procinfos[procinfo_index].lastreturned = \
                            self.procinfos[procinfo_index].server.daq.init(self.procinfos[procinfo_index].fhicl_used + self.get_run_documents() )

                elif command == "Start":
                    self.procinfos[procinfo_index].lastreturned = \
                        self.procinfos[procinfo_index].server.daq.start(\
                        str(self.run_params["run_number"]))
                elif command == "Pause":
                    self.procinfos[procinfo_index].lastreturned = \
                        self.procinfos[procinfo_index].server.daq.pause()
                elif command == "Resume":
                    self.procinfos[procinfo_index].lastreturned = \
                        self.procinfos[procinfo_index].server.daq.resume()
                elif command == "Stop":
                    self.procinfos[procinfo_index].lastreturned = \
                        self.procinfos[procinfo_index].server.daq.stop()
                else:
                    raise Exception("Unknown command")
            except Exception:
                self.exception = True

                pi = self.procinfos[procinfo_index]

                output_message = "DAQInterface caught an exception in " \
                    "process_command() for artdaq process %s at %s : %s \n" % \
                    (pi.name, pi.host, pi.port) + \
                    traceback.format_exc()
                self.print_log(output_message)
            
            return  # From process_command

        # JCF, Nov-8-2015

        # In the code below, transition commands are sent
        # simultaneously only to classes of artdaq type. So, e.g., if
        # we're stopping, first we send stop to all the boardreaders,
        # next we send stop to all the eventbuilders, and finally we
        # send stop to all the aggregators

        proctypes_in_order = ["Aggregator", "EventBuilder","BoardReader"]

        if command == "Stop" or command == "Pause" or command == "Terminate":
            proctypes_in_order.reverse()

        for proctype in proctypes_in_order:

            threads = []

            for i_procinfo, procinfo in enumerate(self.procinfos):
                if proctype in procinfo.name:
                    t = Thread(target=process_command, args=(self, i_procinfo, command))
                    threads.append(t)
                    t.start()

            for thread in threads:
                t.join()

            if self.exception:
                self.alert_and_recover("An exception was thrown "
                                       "during the %s transition" % (command))
                return

        sleep(1)

        if self.debug_level >= 1:
            for procinfo in self.procinfos:
                print "%s, returned string is: " % (procinfo.name)
                print procinfo.lastreturned
                print

        self.check_proc_errors()

        if command != "Init" and command != "Start" and command != "Stop":

            verbing=""

            if command == "Pause":
                verbing = "pausing"
            elif command == "Resume":
                verbing = "resuming"
            else:
                raise Exception("Unknown command")

            self.complete_state_change(self.name, verbing)
            print "\n%s: %s transition complete; if running DAQInterface " % \
                (self.date_and_time(), command) + \
                "in the background, can press <enter> to return to shell prompt"


    # do_initialize(), do_start_running(), etc., are the functions
    # which get called in the runner() function when a transition is
    # requested

    def do_initialize(self):

        self.exception = False

        self.nodiskwrite = False
        self.nomonitoring = False

        if "_nodiskwrite" in self.run_params["config"]:
            self.nodiskwrite = True
            self.run_params["config"] = string.replace( self.run_params["config"],
                                                        "_nodiskwrite",
                                                        "" )

        if "_nomonitoring" in self.run_params["config"]:
            self.nomonitoring = True
            self.run_params["config"] = string.replace( self.run_params["config"],
                                                        "_nomonitoring",
                                                        "" )

        # Reset the DAQInterface configuration variables to their
        # default values in case the configuration file has been
        # changed since the last initialization

        self.reset_DAQInterface_config()

        # The name of the logfile isn't determined until pmt.rb has
        # been run
        self.log_filename_wildcard = None

        self.boardreader_log_filenames = []
        self.eventbuilder_log_filenames = []
        self.aggregator_log_filenames = []

        # JCF, 11/6/14

        self.procinfos = []    # Zero this out in case already filled

        try:
            self.read_DAQInterface_config()
        except Exception:
            self.print_log("DAQInterface caught an "
                           "exception thrown by read_DAQInterface_config()")
            self.print_log(traceback.format_exc())

            self.alert_and_recover("A problem occurred with "
                                   "the configuration manager")
            return

        self.config_dirname, self.fhicl_file_path = self.get_config_info()

        # JCF, 6/11/15

        # I imagine the value of "includes_commit" may change over
        # time, but the idea is to print a warning if DAQInterface
        # detects a version of lbne-artdaq older than some of its
        # functionality uses (e.g., the check_proc_exceptions()
        # function, which requires Kurt's error reporting capability
        # in artdaq)

        includes_commit = "eb2a186e959430f09e7f85e445edce11965f1a7f"
        commit_date = "Jul 14, 2016"

        cmds = []
        cmds.append("cd %s/../lbne-artdaq" % (self.lbneartdaq_build_dir))
        cmds.append("git log | grep %s" % (includes_commit))

        proc = Popen(";".join(cmds), shell=True,
                     stdout=subprocess.PIPE)
        proclines = proc.stdout.readlines()

        if len(proclines) != 1:
            print
            self.print_log("WARNING: DAQInterface expects a version of"
                           " lbne-artdaq as new as or newer than %s (%s);"
                           " %s appears to be older" %
                           (includes_commit, commit_date,
                            self.lbneartdaq_build_dir))
            sleep(5)

        # Save the commit hashes of lbne-artdaq, lbnerc, and the
        # configuration directory, so they can be saved as metadata at
        # the start of the run

        # Dec-9-2015: add in the lbne-raw-data hash as well

        self.lbne_artdaq_hash = self.get_commit_hash(
            "%s/../lbne-artdaq" % (self.lbneartdaq_build_dir))
        if self.lbne_artdaq_hash is None:
            return

        self.lbne_raw_data_hash = self.get_commit_hash(
            "%s/../lbne-raw-data" % (self.lbneartdaq_build_dir))
        if self.lbne_raw_data_hash is None:
            return

        self.artdaq_hash = self.get_commit_hash(
            "%s/../artdaq" % (self.lbneartdaq_build_dir))
        if self.artdaq_hash is None:
            return

#        self.lbnerc_hash = self.get_commit_hash(".")
#        if self.lbnerc_hash is None:
#            return

        self.config_dirname_hash = self.get_commit_hash(self.config_dirname, False)
        if self.config_dirname_hash is None:
            return

        if self.debug_level >= 1:
            print "%s: DAQInterface: \"Init\" transition underway" % \
                (self.date_and_time())

        self.print_log("Config name: %s" % self.run_params["config"], 1)
        self.print_log("Selected DAQ comps: %s" %
                       self.run_params["daq_comp_list"], 1)

        if self.debug_level >= 1:
            for compname, socket in self.run_params["daq_comp_list"].items():
                print "%s at %s:%s" % (compname, socket[0], socket[1])

        # Now contact the configuration manager, if running, for the
        # list of URIs

        try:

            for component in self.run_params["daq_comp_list"].keys():

                if self.debug_level >= 1:
                    print "Searching for the FHiCL document for " + component + \
                        " given configuration \"" + self.run_params["config"] + \
                        "\""

                #manual_uri = "/home/nfs/dunedaq/daqarea/config/" + self.run_params["config"] + "/" + component + "_hw_cfg.fcl"
                manual_uri = self.config_dirname + "/" + self.run_params["config"] + "/" + component + "_hw_cfg.fcl"
                print "WARNING: this is a special version of DAQInterface which skips use of configuration manager; will assume URI is \"" + manual_uri + "\""

                socket = self.run_params["daq_comp_list"][component]

                self.procinfos.append(self.Procinfo("BoardReader",
                                                    socket[0],
                                                    socket[1],
                                                    manual_uri,
                                                    self.fhicl_file_path))
                                          
            # JCF, 11/6/14

            # After a discussion with Kurt this morning, we decided
            # for the time being to stash the two aggregator FHiCL
            # documents as "Aggregator1.fcl" and "Aggregator2.fcl" in
            # a given configuration's directory

            # JCF, 3/19/15

            # Now, handle the FHiCLs for the EventBuilderMains in
            # a similar manner

            # JCF, Jul-5-2016

            # Now that lbne-artdaq is based on artdaq v1_13_00,
            # support >2 aggregators

            support_tuples = [("Aggregator", self.num_aggregators()),
                              ("EventBuilder", self.num_eventbuilders())]

            for support_tuple in support_tuples:

                proc_type, num_procs = support_tuple

                aggregator_cntr = 0
                rootfile_cntr = 0

                for i_proc in range(len(self.procinfos)):

                    if self.procinfos[i_proc].name == proc_type:
                        if self.procinfos[i_proc].fhicl is not None:
                            raise Exception("FHiCL code should not " +
                                            "be supplied for " +
                                            proc_type + "Mains")

                        if proc_type == "EventBuilder":
                            fcl = "%s/%s/EventBuilder1.fcl" % (self.config_dirname,                             
                                                               self.run_params["config"])
                        elif proc_type == "Aggregator":
                            aggregator_cntr += 1
                            if aggregator_cntr < num_procs:
                                fcl = "%s/%s/Aggregator1.fcl" % (self.config_dirname,                             
                                                                 self.run_params["config"])
                            else:
                                fcl = "%s/%s/Aggregator2.fcl" % (self.config_dirname,                             
                                                                 self.run_params["config"])

                        self.procinfos[i_proc].update_fhicl(fcl)
                            
                        fhicl_before_sub = self.procinfos[i_proc].fhicl_used
                        self.procinfos[i_proc].fhicl_used = re.sub("\.root", "_" + str(rootfile_cntr) + ".root",
                                                                   self.procinfos[i_proc].fhicl_used)

                        if self.procinfos[i_proc].fhicl_used != fhicl_before_sub:
                            rootfile_cntr += 1

        except Exception:
            self.print_log("DAQInterface caught an exception " +
                           "in do_initialize()")
            self.print_log(traceback.format_exc())

            self.alert_and_recover("A problem occurred with "
                                   "the configuration manager")
            return

        # See the Procinfo.__lt__ function for details on sorting

        self.procinfos.sort()

        # JCF, 11/11/14

        # Now, set some variables which we'll use to replace
        # pre-existing variables in the FHiCL documents before we send
        # them to the artdaq processes

        # First passthrough of procinfos: assemble the
        # xmlrpc_client_list string, and figure out how many of each
        # type of process there are

        xmlrpc_client_list = "\""
        numeral = ""

        for procinfo in self.procinfos:
            if "BoardReader" in procinfo.name:
                numeral = "3"
            elif "EventBuilder" in procinfo.name:
                numeral = "4"
            elif "Aggregator" in procinfo.name:
                numeral = "5"

            xmlrpc_client_list += ";http://" + procinfo.host + ":" + \
                procinfo.port + "/RPC2," + numeral

        xmlrpc_client_list += "\""

        # Second passthrough: use this newfound info to modify the
        # FHiCL code we'll send during the init transition

        # Note that loops of the form "proc in self.procinfos" are
        # pass-by-value rather than pass-by-reference, so I need to
        # adopt a slightly cumbersome indexing notation

        # JCF, Dec-16-2015

        # Also use this loop to adjust FHiCL code based on whether the
        # user wishes to eliminate writing to disk and/or performing
        # online monitoring

        removed_diskwrite = False
        removed_monitoring = False

        for i_proc in range(len(self.procinfos)):

            self.procinfos[i_proc].fhicl_used = re.sub(
                "first_event_builder_rank.*\n",
                "first_event_builder_rank: " +
                str(self.num_boardreaders()) + "\n",
                self.procinfos[i_proc].fhicl_used)

            self.procinfos[i_proc].fhicl_used = re.sub(
                "event_builder_count.*\n",
                "event_builder_count: " +
                str(self.num_eventbuilders()) + "\n",
                self.procinfos[i_proc].fhicl_used)

            self.procinfos[i_proc].fhicl_used = re.sub(
                "xmlrpc_client_list.*\n",
                "xmlrpc_client_list: " +
                xmlrpc_client_list + "\n",
                self.procinfos[i_proc].fhicl_used)

            self.procinfos[i_proc].fhicl_used = re.sub(
                "first_data_receiver_rank.*\n",
                "first_data_receiver_rank: " +
                str(self.num_boardreaders() +
                    self.num_eventbuilders()) + "\n",
                self.procinfos[i_proc].fhicl_used)

            self.procinfos[i_proc].fhicl_used = re.sub(
                "expected_fragments_per_event.*\n",
                "expected_fragments_per_event: " +
                str(self.num_boardreaders()) + "\n",
                self.procinfos[i_proc].fhicl_used)

            self.procinfos[i_proc].fhicl_used = re.sub(
                "fragment_receiver_count.*\n",
                "fragment_receiver_count: " +
                str(self.num_boardreaders()) + "\n",
                self.procinfos[i_proc].fhicl_used)

            self.procinfos[i_proc].fhicl_used = re.sub(
                "data_receiver_count.*\n",
                "data_receiver_count: " +
                str(self.num_aggregators() - 1) + "\n",
                self.procinfos[i_proc].fhicl_used)

            # JCF, Dec-16-2015

            # Fairly specific assumptions are made about the form of
            # the FHiCL code, thus it's important to record via the
            # "removed_diskwrite" and "removed_monitoring" variables
            # if and when we find the FHiCL code we assume keeps
            # monitoring and/or diskwriting

            if self.nodiskwrite:

                fhicl_before_command = self.procinfos[i_proc].fhicl_used

                self.procinfos[i_proc].fhicl_used = re.sub(
                    "\[\s*p1\s*,\s*e1\s*,\s*a1\s*\]", "[ p1, a1 ]", fhicl_before_command)

                if fhicl_before_command != self.procinfos[i_proc].fhicl_used:
                    removed_diskwrite = True

            if self.nomonitoring:

                fhicl_before_command = self.procinfos[i_proc].fhicl_used

                self.procinfos[i_proc].fhicl_used = re.sub(
                    "\[\s*monitoring\s*\]", "[ ]", fhicl_before_command)

                if fhicl_before_command != self.procinfos[i_proc].fhicl_used:
                    removed_monitoring = True


        if self.nodiskwrite and not removed_diskwrite:
            self.print_log("WARNING: unable to remove diskwriting as "
                           "requested due to unexpected FHiCL code format")

        if self.nomonitoring and not removed_monitoring:
            self.print_log("WARNING: unable to remove monitoring as "
                           "requested due to unexpected FHiCL code format")

        # Now, with the info on hand about the processes contained in
        # procinfos, actually launch them

        try:
            self.launch_procs()

            if self.debug_level >= 1:
                print "Finished call to launch_procs(); will now check that artdaq processes are up..."

        except Exception:
            self.print_log("DAQInterface caught an exception" +
                           "in do_initialize()")
            self.print_log(traceback.format_exc())

            self.alert_and_recover("An exception was thrown in launch_procs()")
            return

        if self.debug_level >= 2:
            print
            print
            print "Output of DAQInterface configuration file %s is: " % \
                (self.config_filename)

            print "/"*70

            for line in open(self.config_filename):
                print "/ %s" % (line),

            print "/"*70
            print
            print

        num_launch_procs_checks = 0

        while True:

            num_launch_procs_checks += 1

            # "False" here means "don't consider it an error if all
            # processes aren't found"

            if self.check_proc_heartbeats(False):

                if self.debug_level > 0:
                    print "All processes appear to be up" + \
                        ", will wait " + \
                        str(self.pause_before_initialization) + \
                        " seconds before initializing..."

                break
            else:
                if self.debug_level > -1 and num_launch_procs_checks > 5:
                    self.print_log("artdaq processes failed to launch")

                    self.alert_and_recover("artdaq processes failed to launch")
                    return

        sleep(self.pause_before_initialization)

        for procinfo in self.procinfos:
            if self.debug_level >= 1:
                print "Initializing " + procinfo.name + \
                    " with " + procinfo.socketstring

            if self.debug_level >= 2:
                print "Sending " + procinfo.socketstring + \
                    " at " + str(time())

            procinfo.server = TimeoutServerProxy(
                procinfo.socketstring, 30)


        self.do_command("Init")

        # JCF, 3/5/15

        # Get our hands on the name of logfile so we can save its
        # name for posterity. This is taken to be the most recent
        # logfile found in the log directory. There's a tiny chance
        # someone else's logfile could sneak in during the few seconds
        # taken during startup, but it's unlikely...

        try:

            log_filename_current = self.get_logfilenames("pmt", 1)[0]
            self.log_filename_wildcard = \
                log_filename_current.split(".")[0] + ".*" + ".log"

            self.boardreader_log_filenames = self.get_logfilenames(
                "boardreader", self.num_boardreaders())

            self.eventbuilder_log_filenames = self.get_logfilenames(
                "eventbuilder", self.num_eventbuilders())

            self.aggregator_log_filenames = self.get_logfilenames(
                "aggregator", self.num_aggregators())

        except Exception:
            self.print_log("DAQInterface caught an exception " +
                           "in do_initialize()")
            self.print_log(traceback.format_exc())
            self.alert_and_recover("Problem obtaining logfile name(s)")
            return

        self.complete_state_change(self.name, "initializing")

        self.display_lbne_artdaq_output()

        if self.debug_level >= 1:
            print "To see logfile(s), on %s run \"ls -ltr %s/pmt/%s\"" % \
                (self.pmt_host, self.log_directory,
                 self.log_filename_wildcard)

        print "\n%s: Initialize transition complete; if running DAQInterface " % \
            (self.date_and_time()) + \
            "in the background, can press <enter> to return to shell prompt"


    def do_start_running(self):

        if self.debug_level >= 1:
            print "%s: DAQInterface: \"Start\" transition underway for run %d" % \
                    (self.date_and_time(), self.run_params["run_number"])

        self.save_run_record()
        self.put_config_info()

        self.do_command("Start")

        # try:
        #     self.attempt_lcm_pulse("start")
        # except Exception:
        #     self.print_log("DAQInterface caught an exception " +
        #                    "in do_start_running()")
        #     self.print_log(traceback.format_exc())

        #     self.print_log("%s, returned string is: " % (procinfo.name,))
        #     self.print_log(procinfo.lastreturned)

        #     self.alert_and_recover("An exception was "
        #                            "thrown during the start transition")
        #     return

        # try:
        #     self.attempt_sync_pulse()
        # except Exception:
        #     self.print_log("DAQInterface caught an exception " +
        #                    "in do_start_running()")
        #     self.print_log(traceback.format_exc())

        #     self.print_log("%s, returned string is: " % (procinfo.name,))
        #     self.print_log(procinfo.lastreturned)

        #     self.alert_and_recover("An exception was "
        #                            "thrown during the start transition")
        #     return

        self.complete_state_change(self.name, "starting")
        print "\n%s: Start transition complete for run %s"  % \
            (self.date_and_time(), str(self.run_params["run_number"])) + \
            ", if running DAQInterface in " + \
            "the background, can press <enter> to return to shell prompt"

    def do_stop_running(self):

        if self.debug_level >= 1:
            print "%s: DAQInterface: \"Stop\" transition underway" % \
                (self.date_and_time())

        # try:
        #     self.attempt_lcm_pulse("stop")
        # except:
        #     self.print_log("DAQInterface caught an exception " +
        #                    "in do_stop_running()")
        #     self.print_log(traceback.format_exc())

        #     self.print_log("%s, returned string is: " % (procinfo.name,))
        #     self.print_log(procinfo.lastreturned)

        #     self.alert_and_recover("An exception was "
        #                            "thrown during the stop transition")
        #     return

        self.do_command("Stop")

        self.complete_state_change(self.name, "stopping")
        print "\n%s: Stop transition complete for run %s; if running DAQInterface " % \
            (self.date_and_time(), str(self.run_params["run_number"]) ) + \
            "in the background, can press <enter> to return to shell prompt"

    def do_terminate(self):

        if self.debug_level >= 1:
            print "%s: DAQInterface: \"Terminate\" transition underway" % \
                (self.date_and_time())

        print

        for procinfo in self.procinfos:

            try:
                procinfo.lastreturned = procinfo.server.daq.shutdown()
            except Exception:
                self.print_log("DAQInterface caught an exception in "
                               "do_terminate()")
                self.print_log(traceback.format_exc())

                self.print_log("%s, returned string is: " % (procinfo.name,))
                self.print_log(procinfo.lastreturned)

                self.alert_and_recover("An exception was thrown "
                                       "during the terminate transition")
                return
            else:
                if self.debug_level >= 1:
                    print "%s, returned string is: " % (procinfo.name,)
                    print procinfo.lastreturned
                    print

        # JCF, Nov-27-2015
        # Disabling this check until eventbuilders can be sent terminate transitions
        # self.check_proc_errors()

        try:
            self.kill_procs()
        except Exception:
            self.print_log("DAQInterface caught an exception in "
                           "do_terminate()")
            self.print_log(traceback.format_exc())
            self.alert_and_recover("An exception was thrown "
                                   "within kill_procs()")
            return

        self.complete_state_change(self.name, "terminating")

        print "\n%s: Terminate transition complete; if running DAQInterface " % \
            (self.date_and_time()) + \
            "in the background, can press <enter> to return to shell prompt"

        if self.debug_level >= 1:
            print "To see logfile(s), on %s run \"ls -ltr %s/pmt/%s\"" % \
                (self.pmt_host, self.log_directory,
                 self.log_filename_wildcard)

    def do_recover(self):
        print
        print "%s: DAQInterface: \"Recover\" transition underway" % \
            (self.date_and_time())
        print "JCF, Jun-12-2014 -- for now at least, \"Recover\" simply " + \
            "kills the artdaq processes"
        print "JCF, Oct-21-2015 -- have now added an attempted stop transition" 
        print "JCF, Jan-8-2015 -- and now added an attempted terminate transition" 

        def attempted_stop(self, procinfo):

            lastreturned=""

            try:
                lastreturned=procinfo.server.daq.stop()
            except Exception:
                self.print_log("Exception caught during stop transition " +
                               "sent to artdaq process %s " % (procinfo.name) +
                               "at %s : %s during recovery procedure;" % (procinfo.host, procinfo.port) +
                               " ignored as kill command will be sent " +
                               "momentarily")
                return

            if lastreturned != "Success":
                self.print_log("Attempted stop sent to artdaq process %s " % (procinfo.name) +
                               "at %s : %s during recovery procedure" % (procinfo.host, procinfo.port) +
                               " returned \"%s\"; ignored as kill command will be sent momentarily" % (lastreturned))
                return

            try:

                # JCF, Jan-11-2016

                # The following "if" in which eventbuilders aren't
                # sent a shutdown is explained by the comment
                # elsewhere in this program beginning with "As of
                # November 27, 2015"; this "if" should be removed once
                # an artdaq which fixes the issue seen in artdaq
                # v1_12_14 is installed

                if "EventBuilder" not in procinfo.name:
                    procinfo.server.daq.shutdown()

            except Exception:
                self.print_log("Exception caught during terminate transition " +
                               "sent to artdaq process %s " % (procinfo.name) +
                               "at %s : %s during recovery procedure;" % (procinfo.host, procinfo.port) +
                               " ignored as kill command will be sent " +
                               "momentarily")
                return

            return


        threads = []

        for procinfo in self.procinfos:
            t = Thread(target=attempted_stop, args=(self, procinfo))
            threads.append(t)
            t.start()

        for thread in threads:
            t.join()
                
                
        try:
            self.kill_procs()
        except Exception:
            self.print_log(traceback.format_exc())

            self.alert_and_recover("An exception was thrown "
                                   "within kill_procs()")
            return

        self.complete_state_change(self.name, "recovering")

        print "\n%s: Recover transition complete; if running DAQInterface " % \
            (self.date_and_time()) + \
            "in the background, can press <enter> to return to shell prompt"

    # Override of the parent class Component's runner function. As of
    # 5/30/14, called every 1s by control.py

    def runner(self):
        """
        Component "ops" loop.  Called at threading hearbeat frequency,
        currently 1/sec.
        """

        if self.__do_initialize:
            self.do_initialize()
            self.__do_initialize = False

        elif self.__do_start_running:
            self.do_start_running()
            self.__do_start_running = False

        elif self.__do_stop_running:
            #self.do_command("Stop")
            self.do_stop_running()
            self.__do_stop_running = False

        elif self.__do_terminate:
            self.do_terminate()
            self.__do_terminate = False

        elif self.__do_pause_running:
            self.do_command("Pause")
            self.__do_pause_running = False

        elif self.__do_resume_running:
            self.do_command("Resume")
            self.__do_resume_running = False

        elif self.__do_recover:
            self.do_recover()
            self.__do_recover = False

        elif self.state(self.name) != "stopped":
            self.check_proc_heartbeats()
            self.check_proc_exceptions()

            if self.state(self.name) == "running":
                self.display_lbne_artdaq_output()


def get_args():  # no-coverage
    parser = argparse.ArgumentParser(
        description="DAQInterface")
    parser.add_argument("-n", "--name", type=str, dest='name',
                        default="daqint", help="Component name")
    parser.add_argument("-r", "--rpc-port", type=int, dest='rpc_port',
                        default=5570, help="RPC port")
    parser.add_argument("-H", "--rpc-host", type=str, dest='rpc_host',
                        default='localhost', help="This hostname/IP addr")
    parser.add_argument("-c", "--control-host", type=str, dest='control_host',
                        default='localhost', help="Control host")
    parser.add_argument("-f", "--config-filename", type=str,
                        dest='config_filename',
                        default="/".join(__file__.split("/")[:-3]) +
                        "/docs/config.txt",
                        help="DAQInterface configuration file")
    return parser.parse_args()


def main():  # no-coverage

    args = get_args()

    with DAQInterface(logpath=os.path.join(os.environ["HOME"], ".lbnedaqint.log"),
                      **vars(args)):
        try:
            while True:
                sleep(100)
        except: KeyboardInterrupt

#        wait_for_interrupt()


# JCF, Nov-16-2016

# Uncomment main() in order to run daqinterface.py as a standalone app (divorced from RC)

main()
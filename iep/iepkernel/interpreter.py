# -*- coding: utf-8 -*-
# Copyright (C) 2013, the IEP development team
#
# IEP is distributed under the terms of the (new) BSD License.
# The full license can be found in 'license.txt'.


""" Module iepkernel.interpreter

Implements the IEP interpreter.

Notes on IPython
----------------
We integrate IPython via the IPython.core.interactiveshell.InteractiveShell.
  * The namespace is set to __main__
  * We call its run_cell method to execute code
  * Debugging/breakpoints are "enabled using the pre_run_code_hook
  * Debugging occurs in our own debugger
  * GUI integration is all handled by IEP
  * We need special prompts for IPython input
  
  

"""

import os, sys, time
import struct
from codeop import CommandCompiler
import traceback
import keyword
import inspect # Must be in this namespace
import bdb

import yoton
from iepkernel import guiintegration, printDirect
from iepkernel.magic import Magician
from iepkernel.debug import Debugger

# Init last traceback information
sys.last_type = None
sys.last_value = None
sys.last_traceback = None

# Set Python version and get some names
PYTHON_VERSION = sys.version_info[0]
if PYTHON_VERSION < 3:
    ustr = unicode
    bstr = str
else:
    ustr = str
    bstr = bytes


# TODO: IPYTHON
# hooks to install on _ipython: editor, ?
# correct line numbers when code is in a cell



class PS1:
    """ Dynamic prompt for PS1. Show IPython prompt if available, and
    show current stack frame when debugging.
    """
    def __init__(self, iep):
        self._iep = iep
    def __str__(self):
        if self._iep._dbFrames:
            # When debugging, show where we are, do not use IPython prompt
            preamble = '('+self._iep._dbFrameName+')'
            return '\x1b[0;32m%s>>> ' % preamble
        elif self._iep._ipython:
            # IPython prompt
            return '\x1b[0;32mIn [\x1b[1;32m%i\x1b[0;32m]: ' % (
                                            self._iep._ipython.execution_count)
            #return 'In [%i]: ' % (self._ipython.execution_count)
        else:
            # Normal Python prompt
            return '\x1b[0;32m>>> '


class PS2:
    """ Dynamic prompt for PS2.
    """
    def __init__(self, iep):
        self._iep = iep
    def __str__(self):
        if self._iep._dbFrames:
            # When debugging, show where we are, do not use IPython prompt
            preamble = '('+self._iep._dbFrameName+')'
            return '\x1b[0;32m%s... ' % preamble
        elif self._iep._ipython:
            # Dots ala IPython
            nspaces = len(str(self._iep._ipython.execution_count)) + 2
            return '\x1b[0;32m%s...: ' % (nspaces*' ')
        else:
            # Just dots
            return '\x1b[0;32m... '

 

class IepInterpreter:
    """ IepInterpreter
    
    The IEP interpreter is the part that makes the IEP kernel interactive.
    It executes code, integrates the GUI toolkit, parses magic commands, etc.
    The IEP interpreter has been designed to emulate the standard interactive
    Python console as much as possible, but with a lot of extra goodies.
    
    There is one instance of this class, stored at sys._iepInterpreter and
    at the __iep__ variable in the global namespace.
    
    The global instance has a couple of interesting attributes:
      * context: the yoton Context instance at the kernel (has all channels)
      * introspector: the introspector instance (a subclassed yoton.RepChannel)
      * magician: the object that handles the magic commands
      * guiApp: a wrapper for the integrated GUI application
      * sleeptime: the amount of time (in seconds) to sleep at each iteration
    
    """
    
    # Simular working as code.InteractiveConsole. Some code was copied, but
    # the following things are changed:
    # - prompts are printed in the err stream, like the default interpreter does
    # - uses an asynchronous read using the yoton interface
    # - support for hijacking GUI toolkits
    # - can run large pieces of code
    # - support post mortem debugging
    # - support for magic commands
    
    def __init__(self, locals, filename="<console>"):
        
        # Init variables for locals and globals (globals only for debugging)
        self.locals = locals
        self.globals = None
        
        # Store filename
        self._filename = filename
        
        # Store ref of locals that is our main
        self._main_locals = locals
        
        # Information for debugging. If self._dbFrames, we're in debug mode
        # _dbFrameIndex starts from 1 
        self._dbFrames = []
        self._dbFrameIndex = 0
        self._dbFrameName = ''
        
        # Init datase to store source code that we execute
        self._codeCollection = ExecutedSourceCollection()
        
        # Init buffer to deal with multi-line command in the shell
        self._buffer = []
        
        # Init sleep time. 0.001 result in 0% CPU usage at my laptop (Windows),
        # but 8% CPU usage at my older laptop (on Linux).
        self.sleeptime = 0.01 # 100 Hz
        
        # Create compiler
        self._compile = CommandCompiler()
        
        # Instantiate magician and tracer
        self.magician = Magician()
        self.debugger = Debugger()
        
        # Define prompts
        try:
            sys.ps1
        except AttributeError:
            sys.ps1 = ">>> "
        try:
            sys.ps2
        except AttributeError:
            sys.ps2 = "... "
        
        # To keep track of whether to send a new prompt, and whether more
        # code is expected.
        self.more = 0
        self.newPrompt = True
        
        # Remove "THIS" directory from the PYTHONPATH
        # to prevent unwanted imports. Same for iepkernel dir
        thisPath = os.getcwd()
        for p in [thisPath, os.path.join(thisPath,'iepkernel')]:
            while p in sys.path:
                sys.path.remove(p)
    
    
    def run(self):    
        """ Run (start the mainloop)
        
        Here we enter the main loop, which is provided by the guiApp. 
        This event loop calls process_commands on a regular basis. 
        
        We may also enter the debug intereaction loop, either from a
        request for post-mortem debugging, or *during* execution by
        means of a breakpoint. When in this debug-loop, the guiApp event
        loop lays still, but the debug-loop does call process-commands
        for user interaction. 
        
        When the user wants to quit, SystemExit is raised (one way or
        another). This is detected in process_commands and the exception
        instance is stored in self._exitException. Then the debug-loop
        is stopped if necessary, and the guiApp is told to stop its event
        loop.
        
        And that brings us back here, where we exit using in order of
        preference: self._exitException, the exception with which the
        event loop was exited (if any), or a new exception.
        
        """
        
        # Prepare
        self._prepare()
        self._exitException = None
        
        # Enter main
        try:
            self.guiApp.run(self.process_commands, self.sleeptime) 
        except SystemExit:
            # Set self._exitException if it is not set yet
            type, value, tb = sys.exc_info();  del tb
            if self._exitException is None:
                self._exitException = value
        
        # Exit
        if self._exitException is None:
            self._exitException = SystemExit()
        raise self._exitException
    
    
    def _prepare(self):
        """ Prepare for running the main loop.
        Here we do some initialization like obtaining the startup info,
        creating the GUI application wrapper, etc.
        """
        
        # Reset debug status
        self.debugger.writestatus()
        
        # Get startup info (get a copy, or setting the new version wont trigger!)
        while self.context._stat_startup.recv() is None:
            time.sleep(0.02)
        self.startup_info = startup_info = self.context._stat_startup.recv().copy()
        
        # Set startup info (with additional info)
        builtins = __builtins__
        if not isinstance(builtins, dict):
            builtins = builtins.__dict__
        startup_info['builtins'] = [builtin for builtin in builtins.keys()]
        startup_info['version'] = tuple(sys.version_info)
        startup_info['keywords'] = keyword.kwlist
        self.context._stat_startup.send(startup_info)
        
        # Write Python banner (to stdout)
        NBITS = 8 * struct.calcsize("P")
        platform = sys.platform
        if platform.startswith('win'):
            platform = 'Windows'
        platform = '%s (%i bits)' % (platform, NBITS) 
        printDirect("Python %s on %s.\n" %
            (sys.version.split('[')[0].rstrip(), platform))
        
        
        # Integrate event loop of GUI toolkit
        self.guiApp = guiintegration.App_base()
        self.guiName = guiName = startup_info['gui'].upper()
        guiError = ''
        try:
            if guiName in ['', 'NONE']:
                guiName = ''
            elif guiName == 'TK':
                self.guiApp = guiintegration.App_tk()
            elif guiName == 'WX':
                self.guiApp = guiintegration.App_wx()
            elif guiName == 'PYSIDE':
                self.guiApp = guiintegration.App_pyside()
            elif guiName in ['PYQT4', 'QT4']:
                self.guiApp = guiintegration.App_pyqt4()
            elif guiName == 'FLTK':
                self.guiApp = guiintegration.App_fltk()
            elif guiName == 'GTK':
                self.guiApp = guiintegration.App_gtk()
            else:
                guiError = 'Unkown gui: %s' % guiName
                guiName = ''
        except Exception: # Catch any error
            # Get exception info (we do it using sys.exc_info() because
            # we cannot catch the exception in a version independent way.
            type, value, tb = sys.exc_info();  del tb
            guiError = 'Failed to integrate event loop for %s: %s' % (
                guiName, str(value))
        
        # Write IEP part of banner (including what GUI loop is integrated)
        if True:
            iepBanner = 'This is the IEP interpreter'
        if guiError:
            iepBanner += '. ' + guiError + '\n'
        elif guiName:
            iepBanner += ' with integrated event loop for ' 
            iepBanner += guiName + '.\n'
        else:
            iepBanner += '.\n'
        printDirect(iepBanner)
        
        # Load IPython
        self._ipython = None
        # todo: disabled because work in progress
        try:
            #import invoke_import_error
            import __main__
            from IPython.core.interactiveshell import InteractiveShell 
            self._ipython = InteractiveShell(user_module=__main__)
            self._ipython.set_hook('pre_run_code_hook', self.ipython_pre_run_code_hook)
            self._ipython.set_custom_exc((bdb.BdbQuit,), self.dbstop_handler)
        except ImportError:
            pass
        except Exception:
            print('could not use IPython')
        
        # Set prompts
        sys.ps1 = PS1(self)
        sys.ps2 = PS2(self)
        
        # Append project path if given
        projectPath = startup_info['projectPath']
        if projectPath:
            printDirect('Prepending the project path %r to sys.path\n' % 
                projectPath)
            #Actual prepending is done below, to put it before the script path
        
        # Write tips message
        printDirect('Type "help" for help, ' + 
                            'type "?" for a list of *magic* commands.\n')
        
        
        # Get whether we should (and can) run as script
        scriptFilename = startup_info['scriptFile']
        if scriptFilename:
            if not os.path.isfile(scriptFilename):
                printDirect('Invalid script file: "'+scriptFilename+'"\n')
                scriptFilename = None
        
        # Init script to run on startup
        self._scriptToRunOnStartup = None
        
        if scriptFilename:
            # RUN AS SCRIPT
            
            # Set __file__  (note that __name__ is already '__main__')
            self.locals['__file__'] = scriptFilename
            # Set command line arguments
            sys.argv[:] = []
            sys.argv.append(scriptFilename)
            # Insert script directory to path
            theDir = os.path.abspath( os.path.dirname(scriptFilename) )
            if theDir not in sys.path:
                sys.path.insert(0, theDir)
            if projectPath is not None:
                sys.path.insert(0,projectPath)
            
            # Go to script dir
            os.chdir( os.path.dirname(scriptFilename) )
            
            # Notify the running of the script
            printDirect('[Running script: "'+scriptFilename+'"]\n')
            
            # Run script
            self._scriptToRunOnStartup = scriptFilename
        
        else:
            # RUN INTERACTIVELY
            
            # No __file__ (note that __name__ is already '__main__')
            self.locals.pop('__file__','')
            # Remove all command line arguments, set first to empty string
            sys.argv[:] = []
            sys.argv.append('')
            # Insert current directory to path
            sys.path.insert(0, '')
            if projectPath:
                sys.path.insert(0,projectPath)
                
            # Go to start dir
            startDir = startup_info['startDir']
            if startDir and os.path.isdir(startDir):
                os.chdir(startDir)
            else:
                os.chdir(os.path.expanduser('~')) # home dir 
            
            # Run startup script (if set)
            filename = startup_info['startupScript']
            # Should we use the default startupScript?
            if filename == '$PYTHONSTARTUP':
                filename = os.environ.get('PYTHONSTARTUP','')
            # Check if it exists
            if filename and os.path.isfile(filename):
                self._scriptToRunOnStartup = filename
    
    
    def process_commands(self):
        """ Do one iteration of processing commands (the REPL).
        """
        try:
            
            self._process_commands()
        
        except KeyboardInterrupt:
            self.write("\nKeyboardInterrupt\n")
            self._resetbuffer()
            self.more = 0
        except TypeError:
            # For some reason, when wx is integrated, keyboard interrupts
            # result in a TypeError.
            # I tried to find the source, but did not find it. If anyone
            # has an idea, please e-mail me!
            if self.guiName == 'WX':
                self.write("\nKeyboard Interrupt\n") # space to see difference
                self._resetbuffer()
                self.more = 0
        except SystemExit:
            # Get and store the exception so we can raise it later
            type, value, tb = sys.exc_info();  del tb
            self._exitException = value
            # Stop debugger if it is running
            self.debugger.stopinteraction()
            # Exit from interpreter. Exit in the appropriate way
            self.guiApp.quit()  # Is sys.exit() by default
    
    
    def _process_commands(self):
        
        # Run startup script inside the loop (only the first time)
        # so that keyboard interrupt will work
        if self._scriptToRunOnStartup:
            self.context._stat_interpreter.send('Busy') 
            self._scriptToRunOnStartup, tmp = None, self._scriptToRunOnStartup
            self.runfile(tmp)
        
        # Set status and prompt?
        # Prompt is allowed to be an object with __str__ method
        if self.newPrompt:
            self.newPrompt = False
            ps = sys.ps2 if self.more else sys.ps1
            self.context._strm_prompt.send(str(ps))
        
        if True:
            # Determine state. The message is really only send
            # when the state is different. Note that the kernelbroker
            # can also set the state ("Very busy", "Busy", "Dead")
            if self._dbFrames:
                self.context._stat_interpreter.send('Debug')
            elif self.more:
                self.context._stat_interpreter.send('More')
            else:
                self.context._stat_interpreter.send('Ready')
        
        
        # Are we still connected?
        if sys.stdin.closed or not self.context.connection_count:
            # Exit from main loop.
            # This will raise SystemExit and will shut us down in the 
            # most appropriate way
            sys.exit()
        
        # Get channel to take a message from
        ch = yoton.select_sub_channel(self.context._ctrl_command, self.context._ctrl_code)
        
        if ch is None:
            pass # No messages waiting
        
        elif ch is self.context._ctrl_command:
            # Read command 
            line1 = self.context._ctrl_command.recv(False) # Command
            if line1:
                # Notify what we're doing
                self.context._strm_echo.send(line1)
                self.context._stat_interpreter.send('Busy')
                self.newPrompt = True
                # Convert command
                line2 = self.magician.convert_command(line1.rstrip('\n'))
                # Execute actual code
                if line2 is not None:
                    for line3 in line2.split('\n'): # not splitlines!
                        self.more = self.pushline(line3)
                else:
                    self.more = False
                    self._resetbuffer()
        
        elif ch is self.context._ctrl_code:
            # Read larger block of code (dict)
            msg = self.context._ctrl_code.recv(False)
            if msg:
                # Notify what we're doing
                # (runlargecode() sends on stdin-echo)
                self.context._stat_interpreter.send('Busy')
                self.newPrompt = True
                # Execute code
                self.runlargecode(msg)
                # Reset more stuff
                self._resetbuffer()
                self.more = False
        
        else:
            # This should not happen, but if it does, just flush!
            ch.recv(False)


    
    ## Running code in various ways
    # In all cases there is a call for compilecode and a call to execcode
    
    def _resetbuffer(self):
        """Reset the input buffer."""
        self._buffer = []
    
    
    def pushline(self, line):
        """Push a line to the interpreter.
        
        The line should not have a trailing newline; it may have
        internal newlines.  The line is appended to a buffer and the
        interpreter's _runlines() method is called with the
        concatenated contents of the buffer as source.  If this
        indicates that the command was executed or invalid, the buffer
        is reset; otherwise, the command is incomplete, and the buffer
        is left as it was after the line was appended.  The return
        value is 1 if more input is required, 0 if the line was dealt
        with in some way (this is the same as _runlines()).
        
        """
        # Get buffer, join to get source
        buffer = self._buffer
        buffer.append(line)
        source = "\n".join(buffer)
        # Clear buffer and run source
        self._resetbuffer()
        more = self._runlines(source, self._filename)
        # Create buffer if needed
        if more:
            self._buffer = buffer 
        return more
    

    def _runlines(self, source, filename="<input>", symbol="single"):
        """Compile and run some source in the interpreter.
        
        Arguments are as for compile_command().
        
        One several things can happen:
        
        1) The input is incorrect; compile_command() raised an
        exception (SyntaxError or OverflowError).  A syntax traceback
        will be printed by calling the showsyntaxerror() method.
        
        2) The input is incomplete, and more input is required;
        compile_command() returned None.  Nothing happens.
        
        3) The input is complete; compile_command() returned a code
        object.  The code is executed by calling self.execcode() (which
        also handles run-time exceptions, except for SystemExit).
        
        The return value is True in case 2, False in the other cases (unless
        an exception is raised).  The return value can be used to
        decide whether to use sys.ps1 or sys.ps2 to prompt the next
        line.
        
        """
        
        use_ipython = self._ipython and not self._dbFrames
        
        # Try compiling.
        # The IPython kernel does not handle incomple lines, so we check
        # that ourselves ...
        try:
            code = self.compilecode(source, filename, symbol)
        except (OverflowError, SyntaxError, ValueError):
            code = False
        
        if use_ipython:
            if code is None:
                # Case 2
                #self._ipython.run_cell('', True)
                return True
            else:
                # Case 1 and 3 handled by IPython
                self._ipython.run_cell(source, True)
                return False
                
        else:
            if code is None:
                # Case 2
                return True
            elif not code:
                # Case 1
                self.showsyntaxerror(filename)
                return False
            else:
                # Case 3
                self.execcode(code)
                return False
    
    
    def runlargecode(self, msg):
        """ To execute larger pieces of code. """
        
        # Get information
        source, fname, lineno = msg['source'], msg['fname'], msg['lineno']
        cellName = msg.get('cellName', '')
        source += '\n'
        
        # Construct notification message
        lineno1 = lineno + 1
        lineno2 = lineno + source.count('\n')
        fname_show = fname
        if not fname.startswith('<'):
            fname_show = os.path.split(fname)[1]
        if cellName:
            runtext = '(executing cell "%s" (line %i of "%s"))\n' % (cellName, lineno1, fname_show)
        elif lineno1 == lineno2:
            runtext = '(executing line %i of "%s")\n' % (lineno1, fname_show)
        else:
            runtext = '(executing lines %i to %i of "%s")\n' % (
                                                lineno1, lineno2, fname_show)
        # Notify IDE
        colorcode = '\x1b[0;33m'
        self.context._strm_echo.send(colorcode+runtext)
        
        # Increase counter
        if self._ipython:
            self._ipython.execution_count += 1
        
        # Put the line number in the filename (if necessary)
        # Note that we could store the line offset in the _codeCollection,
        # but then we cannot retrieve it for syntax errors.
        if lineno:
            fname = "%s+%i" % (fname, lineno)
        
        # Try compiling the source
        code = None
        try:            
            # Compile
            code = self.compilecode(source, fname, "exec")          
            
        except (OverflowError, SyntaxError, ValueError):
            self.showsyntaxerror(fname)
            return
        
        if code:
            # Store the source using the (id of the) code object as a key
            self._codeCollection.storeSource(code, source)
            # Execute the code
            self.execcode(code)
        else:
            # Incomplete code
            self.write('Could not run code because it is incomplete.\n')
    
    
    def runfile(self, fname):
        """  To execute the startup script. """ 
        
        # Get text (make sure it ends with a newline)
        try:
            source = open(fname, 'rb').read().decode('UTF-8')
        except Exception:
            printDirect('Could not read script (decoding using UTF-8): "' + fname + '"\n')
            return
        try:
            source = source.replace('\r\n', '\n').replace('\r','\n')
            if source[-1] != '\n':
                source += '\n'
        except Exception:        
            printDirect('Could not execute script: "' + fname + '"\n')
            return
        
        # Try compiling the source
        code = None
        try:            
            # Compile
            code = self.compilecode(source, fname, "exec")
        except (OverflowError, SyntaxError, ValueError):
            time.sleep(0.2) # Give stdout time to be send
            self.showsyntaxerror(fname)
            return
        
        if code:
            # Store the source using the (id of the) code object as a key
            self._codeCollection.storeSource(code, source)
            # Execute the code
            self.execcode(code)
        else:
            # Incomplete code
            self.write('Could not run code because it is incomplete.\n')
    
    
    def compilecode(self, source, filename, mode, *args, **kwargs):
        """ Compile source code.
        Will mangle coding definitions on first two lines. 
        
        * This method should be called with Unicode sources.
        * Source newlines should consist only of LF characters.
        """
        
        # This method solves IEP issue 22

        # Split in first two lines and the rest
        parts = source.split('\n', 2)
        
        # Replace any coding definitions
        ci = 'coding is'
        contained_coding = False
        for i in range(len(parts)-1):
            tmp = parts[i]
            if tmp and tmp[0] == '#' and 'coding' in tmp:
                contained_coding = True
                parts[i] = tmp.replace('coding=', ci).replace('coding:', ci)
        
        # Combine parts again (if necessary)
        if contained_coding:
            source = '\n'.join(parts)
        
        # Convert filename to UTF-8 if Python version < 3
        if PYTHON_VERSION < 3:
            filename = filename.encode('utf-8')
        
        # Compile
        return self._compile(source, filename, mode, *args, **kwargs)
    
    
    def execcode(self, code):
        """Execute a code object.
        
        When an exception occurs, self.showtraceback() is called to
        display a traceback.  All exceptions are caught except
        SystemExit, which is reraised.
        
        A note about KeyboardInterrupt: this exception may occur
        elsewhere in this code, and may not always be caught.  The
        caller should be prepared to deal with it.
        
        The globals variable is used when in debug mode.
        """
        
        try:
            if self._dbFrames:
                self.apply_breakpoints()
                exec(code, self.globals, self.locals)
            else:
                # Turn debugger on at this point. If there are no breakpoints,
                # the tracing is disabled for better performance.
                self.apply_breakpoints()
                self.debugger.set_on() 
                exec(code, self.locals)
        except bdb.BdbQuit:
            self.dbstop_handler()
        except Exception:
            time.sleep(0.2) # Give stdout some time to send data
            self.showtraceback()
        except KeyboardInterrupt: # is a BaseException, not an Exception
            time.sleep(0.2)
            self.showtraceback()
    
    
    def apply_breakpoints(self):
        """ Breakpoints are updated at each time a command is given,
        including commands like "db continue".
        """
        try:
            breaks = self.context._stat_breakpoints.recv()
            if self.debugger.breaks:
                self.debugger.clear_all_breaks()
            if breaks:  # Can be None
                for fname in breaks:
                    for linenr in breaks[fname]:
                        self.debugger.set_break(fname, linenr)
        except Exception:
            type, value, tb = sys.exc_info(); del tb
            print('Error while setting breakpoints: %s' % str(value))
    
    
    ## Handlers and hooks
    
    def ipython_pre_run_code_hook(self, ipython):
        """ Hook that IPython calls right before executing code.
        """
        self.apply_breakpoints()
        self.debugger.set_on() 
    
    
    def dbstop_handler(self, *args, **kwargs):
        print("Program execution stopped from debugger.")
    
    
    
    
    
    
    ## Writing and error handling
    
    
    def write(self, text):
        """ Write errors. """
        sys.stderr.write( text )
    
    
    def showsyntaxerror(self, filename=None):
        """Display the syntax error that just occurred.
        This doesn't display a stack trace because there isn't one.        
        If a filename is given, it is stuffed in the exception instead
        of what was there before (because Python's parser always uses
        "<string>" when reading from a string).
        
        IEP version: support to display the right line number,
        see doc of showtraceback for details.        
        """
        
        # Get info (do not store)
        type, value, tb = sys.exc_info();  del tb
        
        # Work hard to stuff the correct filename in the exception
        if filename and type is SyntaxError:
            try:
                # unpack information
                msg, (dummy_filename, lineno, offset, line) = value
                # correct line-number
                fname, lineno = self.correctfilenameandlineno(filename, lineno)
            except:
                # Not the format we expect; leave it alone
                pass
            else:
                # Stuff in the right filename
                value = SyntaxError(msg, (fname, lineno, offset, line))
                sys.last_value = value
        
        # Show syntax error 
        strList = traceback.format_exception_only(type, value)
        for s in strList:
            self.write(s)
    
    
    def showtraceback(self, useLastTraceback=False):
        """Display the exception that just occurred.
        We remove the first stack item because it is our own code.
        The output is written by self.write(), below.
        
        In the IEP version, before executing a block of code,
        the filename is modified by appending " [x]". Where x is
        the index in a list that we keep, of tuples 
        (sourcecode, filename, lineno). 
        
        Here, showing the traceback, we check if we see such [x], 
        and if so, we extract the line of code where it went wrong,
        and correct the lineno, so it will point at the right line
        in the editor if part of a file was executed. When the file
        was modified since the part in question was executed, the
        fileno might deviate, but the line of code shown shall 
        always be correct...
        """
        # Traceback info:
        # tb_next -> go down the trace
        # tb_frame -> get the stack frame
        # tb_lineno -> where it went wrong
        #
        # Frame info:
        # f_back -> go up (towards caller)
        # f_code -> code object
        # f_locals -> we can execute code here when PM debugging
        # f_globals
        # f_trace -> (can be None) function for debugging? (
        #
        # The traceback module is used to obtain prints from the
        # traceback.
        
        try:
            if useLastTraceback:
                # Get traceback info from buffered
                type = sys.last_type
                value = sys.last_value
                tb = sys.last_traceback
            else:
                # Get exception information and remove first, since that's us
                type, value, tb = sys.exc_info()
                tb = tb.tb_next
                
                # Store for debugging, but only store if not in debug mode
                if not self._dbFrames:
                    sys.last_type = type
                    sys.last_value = value
                    sys.last_traceback = tb
            
            # Get tpraceback to correct all the line numbers
            # tblist = list  of (filename, line-number, function-name, text)
            tblist = traceback.extract_tb(tb)
            
            # Get frames
            frames = []
            while tb:
                frames.append(tb.tb_frame)
                tb = tb.tb_next
            frames.pop(0)
            
            # Walk through the list
            for i in range(len(tblist)):
                tbInfo = tblist[i]                
                # Get filename and line number, init example
                fname, lineno = self.correctfilenameandlineno(tbInfo[0], tbInfo[1])
                if not isinstance(fname, ustr):
                    fname = fname.decode('utf-8')
                example = tbInfo[3]
                # Get source (if available) and split lines
                source = None
                if i < len(frames):
                    source = self._codeCollection.getSource(frames[i].f_code)
                if source:
                    source = source.splitlines()                
                    # Obtain source from example and select line                    
                    try:
                        example = source[ tbInfo[1]-1 ]
                    except IndexError:
                        pass
                # Reset info
                tblist[i] = (fname, lineno, tbInfo[2], example)
            
            # Format list
            strList = traceback.format_list(tblist)
            if strList:
                strList.insert(0, "Traceback (most recent call last):\n")
            strList.extend( traceback.format_exception_only(type, value) )
            
            # Write traceback
            for s in strList:
                self.write(s)
            
            # Clean up (we cannot combine except and finally in Python <2.5
            tb = None
            frames = None
        
        except Exception:
            self.write('An error occured, but could not write traceback.\n')
            tb = None
            frames = None
    
    
    def correctfilenameandlineno(self, fname, lineno):
        """ Given a filename and lineno, this function returns
        a modified (if necessary) version of the two. 
        As example:
        "foo.py+7", 22  -> "foo.py", 29
        """
        j = fname.find('+')
        if j>0:
            try:
                lineno += int(fname[j+1:])
                fname = fname[:j]
            except ValueError:
                pass
        return fname, lineno


class ExecutedSourceCollection(dict):
    """ Stores the source of executed pieces of code, so that the right 
    traceback can be reproduced when an error occurs.
    The codeObject produced by compiling the source is used as a 
    reference.
    """
    def _getId(self, codeObject):
        id_ = str(id(codeObject)) + '_' + codeObject.co_filename
    def storeSource(self, codeObject, source):
        self[self._getId(codeObject)] = source
    def getSource(self, codeObject):
        return self.get(self._getId(codeObject), '')

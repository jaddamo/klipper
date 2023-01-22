# Add ability to define custom g-code macros
#
# Copyright (C) 2018-2021  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import traceback, logging, ast, copy
import jinja2


######################################################################
# Template handling
######################################################################

# Wrapper for access to printer object get_status() methods
class GetStatusWrapper:
    def __init__(self, printer, eventtime=None):
        self.printer = printer
        self.eventtime = eventtime
        self.cache = {}
    def __getitem__(self, val):
        sval = str(val).strip()
        if sval in self.cache:
            return self.cache[sval]
        po = self.printer.lookup_object(sval, None)
        if po is None or not hasattr(po, 'get_status'):
            raise KeyError(val)
        if self.eventtime is None:
            self.eventtime = self.printer.get_reactor().monotonic()
        self.cache[sval] = res = copy.deepcopy(po.get_status(self.eventtime))
        return res
    def __contains__(self, val):
        try:
            self.__getitem__(val)
        except KeyError as e:
            return False
        return True
    def __iter__(self):
        for name, obj in self.printer.lookup_objects():
            if self.__contains__(name):
                yield name

# Wrapper around a Jinja2 template
class TemplateWrapper:
    def __init__(self, printer, env, name, script):
        self.printer = printer
        self.name = name
        self.gcode = self.printer.lookup_object('gcode')
        gcode_macro = self.printer.lookup_object('gcode_macro')
        self.create_template_context = gcode_macro.create_template_context
        try:
            self.template = env.from_string(script)
        except Exception as e:
            msg = f"Error loading template '{name}': {traceback.format_exception_only(type(e), e)[-1]}"
            logging.exception(msg)
            raise printer.config_error(msg)
    def render(self, context=None):
        if context is None:
            context = self.create_template_context()
        try:
            return str(self.template.render(context))
        except Exception as e:
            msg = f"Error evaluating '{self.name}': {traceback.format_exception_only(type(e), e)[-1]}"
            logging.exception(msg)
            raise self.gcode.error(msg)
    def run_gcode_from_command(self, context=None):
        self.gcode.run_script_from_command(self.render(context))

# Main gcode macro template tracking
class PrinterGCodeMacro:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.env = jinja2.Environment('{%', '%}', '{', '}')
    def load_template(self, config, option, default=None):
        name = f"{config.get_name()}:{option}"
        script = config.get(option) if default is None else config.get(option, default)
        return TemplateWrapper(self.printer, self.env, name, script)
    def _action_emergency_stop(self, msg="action_emergency_stop"):
        self.printer.invoke_shutdown(f"Shutdown due to {msg}")
        return ""
    def _action_respond_info(self, msg):
        self.printer.lookup_object('gcode').respond_info(msg)
        return ""
    def _action_raise_error(self, msg):
        raise self.printer.command_error(msg)
    def _action_call_remote_method(self, method, **kwargs):
        webhooks = self.printer.lookup_object('webhooks')
        try:
            webhooks.call_remote_method(method, **kwargs)
        except self.printer.command_error:
            logging.exception("Remote Call Error")
        return ""
    def create_template_context(self, eventtime=None):
        return {
            'printer': GetStatusWrapper(self.printer, eventtime),
            'action_emergency_stop': self._action_emergency_stop,
            'action_respond_info': self._action_respond_info,
            'action_raise_error': self._action_raise_error,
            'action_call_remote_method': self._action_call_remote_method,
        }

def load_config(config):
    return PrinterGCodeMacro(config)


######################################################################
# GCode macro
######################################################################

class GCodeMacro:
    def __init__(self, config):
        if len(config.get_name().split()) > 2:
            raise config.error(
                f"Name of section '{config.get_name()}' contains illegal whitespace"
            )
        name = config.get_name().split()[1]
        self.alias = name.upper()
        self.printer = printer = config.get_printer()
        gcode_macro = printer.load_object(config, 'gcode_macro')
        self.template = gcode_macro.load_template(config, 'gcode')
        self.gcode = printer.lookup_object('gcode')
        self.rename_existing = config.get("rename_existing", None)
        self.cmd_desc = config.get("description", "G-Code macro")
        if self.rename_existing is None:
            self.gcode.register_command(self.alias, self.cmd,
                                        desc=self.cmd_desc)
        elif (self.gcode.is_traditional_gcode(self.alias)
                != self.gcode.is_traditional_gcode(self.rename_existing)):
            raise config.error(
                f"G-Code macro rename of different types ('{self.alias}' vs '{self.rename_existing}')"
            )
        else:
            printer.register_event_handler("klippy:connect",
                                           self.handle_connect)
        self.gcode.register_mux_command("SET_GCODE_VARIABLE", "MACRO",
                                        name, self.cmd_SET_GCODE_VARIABLE,
                                        desc=self.cmd_SET_GCODE_VARIABLE_help)
        self.in_script = False
        self.variables = {}
        prefix = 'variable_'
        for option in config.get_prefix_options(prefix):
            try:
                self.variables[option[len(prefix):]] = ast.literal_eval(
                    config.get(option))
            except ValueError as e:
                raise config.error(
                    f"Option '{option}' in section '{config.get_name()}' is not a valid literal"
                )
    def handle_connect(self):
        prev_cmd = self.gcode.register_command(self.alias, None)
        if prev_cmd is None:
            raise self.printer.config_error(
                f"Existing command '{self.alias}' not found in gcode_macro rename"
            )
        pdesc = f"Renamed builtin of '{self.alias}'"
        self.gcode.register_command(self.rename_existing, prev_cmd, desc=pdesc)
        self.gcode.register_command(self.alias, self.cmd, desc=self.cmd_desc)
    def get_status(self, eventtime):
        return self.variables
    cmd_SET_GCODE_VARIABLE_help = "Set the value of a G-Code macro variable"
    def cmd_SET_GCODE_VARIABLE(self, gcmd):
        variable = gcmd.get('VARIABLE')
        value = gcmd.get('VALUE')
        if variable not in self.variables:
            raise gcmd.error(f"Unknown gcode_macro variable '{variable}'")
        try:
            literal = ast.literal_eval(value)
        except ValueError as e:
            raise gcmd.error(f"Unable to parse '{value}' as a literal")
        v = dict(self.variables)
        v[variable] = literal
        self.variables = v
    def cmd(self, gcmd):
        if self.in_script:
            raise gcmd.error(f"Macro {self.alias} called recursively")
        kwparams = dict(self.variables)
        kwparams |= self.template.create_template_context()
        kwparams['params'] = gcmd.get_command_parameters()
        kwparams['rawparams'] = gcmd.get_raw_command_parameters()
        self.in_script = True
        try:
            self.template.run_gcode_from_command(kwparams)
        finally:
            self.in_script = False

def load_config_prefix(config):
    return GCodeMacro(config)

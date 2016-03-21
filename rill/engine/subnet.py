from abc import ABCMeta, abstractmethod

from future.utils import with_metaclass

from rill.engine.component import Component, inport, outport
from rill.engine.network import Network
from rill.engine.status import StatusValues
from rill.engine.component import logger
from rill.engine.inputport import InputPort
from rill.engine.outputport import OutputPort
from rill.engine.exceptions import FlowError
from rill.engine.packet import Packet


@inport("NAME", type=str)
@inport("INDEX", type=int)
@outport("OUT")
class SubIn(Component):
    def execute(self):
        pname = self.inports.NAME.receive_once()
        if pname is None:
            return

        if self.outports.OUT.is_closed():
            return

        inport = self.mother.inports[pname]
        index = self.inports.INDEX.receive_once()
        if index is not None:
            inport = inport[index]

        self.trace_funcs("Accessing input port: {}".format(inport))

        # FIXME: this is definitely broken:
        if inport.is_static():
            old_receiver = inport.component
            iic = InitializationConnection(self, inport.name, inport._content)
            # iic.network = iico.network

            p = iic.receive()
            p.set_owner(self)
            self.outports.OUT.send(p)
            iic.close()
        else:
            old_receiver = inport.component
            inport.component = self

            for p in inport:
                p.set_owner(self)
                self.outports.OUT.send(p)

        # inport.close()
        self.trace_funcs("Releasing input port: {}".format(inport))

        inport.component = old_receiver


@inport("NAME", type=str)
@inport("INDEX", type=int)
@outport("OUT")
class SubInSS(Component):
    def execute(self):
        pname = self.inports.NAME.receive_once()

        if self.outports.OUT.is_closed():
            return

        inport = self.mother.inports[pname]
        index = self.inports.INDEX.receive_once()
        if index is not None:
            inport = inport[index]

        self.trace_funcs("Accessing input port: {}".format(inport))

        old_receiver = inport.component
        if inport.is_static():
            raise FlowError("SubinSS cannot support IIP - use Subin")

        inport.component = self
        level = 0
        for p in inport:
            p.set_owner(self)
            if p.get_type() == Packet.OPEN:
                if level > 0:
                    self.outports.OUT.send(p)
                else:
                    self.drop(p)
                    self.trace_funcs("open bracket detected")
                level += 1
            elif p.get_type() == Packet.CLOSE:
                if level > 1:
                    # pass on nested brackets
                    self.outports.OUT.send(p)
                    level -= 1
                else:
                    self.drop(p)
                    self.trace_funcs("close bracket detected")
                    break
            else:
                self.outports.OUT.send(p)

        self.trace_funcs("Releasing input port: {}".format(inport))
        # inport.set_receiver(old_receiver)
        inport.component = old_receiver


@inport("IN")
@inport("NAME", type=str)
@inport("INDEX", type=int)
class SubOut(Component):
    def execute(self):
        pname = self.inports.NAME.receive_once()
        if pname is None:
            return

        outport = self.mother.outports[pname]
        index = self.inports.INDEX.receive_once()
        if index is not None:
            outport = outport[index]

        self.trace_funcs("Accessing output port: {}".format(outport))
        outport.component = self

        for p in self.inports.IN:
            outport.send(p)

        self.trace_funcs("Releasing output port: {}".format(outport))


@inport("IN")
@inport("NAME", type=str)
@inport("INDEX", type=int)
class SubOutSS(Component):
    """
    Look after output from subnet

    Differs from SubOut in that it adds an open bracket at the beginning, and a
    close bracket at the end
    """

    def execute(self):
        pname = self.inports.NAME.receive_once()
        if pname is None:
            return

        outport = self.mother.outports[pname]
        index = self.inports.INDEX.receive_once()
        if index is not None:
            outport = outport[index]

        self.trace_funcs("Accessing output port: {}".format(outport))
        outport.component = self

        p = self.create(Packet.OPEN)
        outport.send(p)

        for p in self.inports.IN:
            outport.send(p)

        p = self.create(Packet.CLOSE)
        outport.send(p)

        self.trace_funcs("Releasing output port: {}".format(outport))


class SubNet(with_metaclass(ABCMeta, Network, Component)):
    def __init__(self, name, mother):
        Component.__init__(self, name, mother)
        Network.__init__(self)

    @abstractmethod
    def define(self):
        raise NotImplementedError

    def export(self, internal_port, external_port):
        """
        Expose a port.

        Parameters
        ----------
        external_port : ``rill.engine.outputport.OutputPort`` or
                        ``rill.engine.inputport.InputPort`` or str
            external SubNet port
        internal_port : ``rill.engine.outputport.OutputPort`` or
                        ``rill.engine.inputport.InputPort`` or str
            internal port
        """
        # FIXME: support exporting an array port to array port. currently,
        # if you try to do that you'll end up exporting the first element
        external_port = self.port(external_port)

        if external_port.is_array():
            external_port = external_port.get_element(create=True)

        if isinstance(external_port, InputPort):
            internal_port = self.get_component_port(internal_port,
                                                    kind='input')
            subcomp = self.add_component('_' + external_port.name, SubIn,
                                         NAME=external_port._name)
            if external_port.index is not None:
                self.initialize(external_port.index, subcomp.inports.INDEX)
            self.connect(subcomp.outports.OUT, internal_port)
        elif isinstance(external_port, OutputPort):
            internal_port = self.get_component_port(internal_port,
                                                    kind='output')
            subcomp = self.add_component('_' + external_port.name, SubOut,
                                         NAME=external_port._name)
            if external_port.index is not None:
                self.initialize(external_port.index, subcomp.inports.INDEX)
            self.connect(internal_port, subcomp.inports.IN)

    def execute(self):
        # FIXME: this, signal_error, and terminate are the only things in
        # SubNet that require it to be a ComponentRunner.  do we need them?
        if self.status != StatusValues.ERROR:

            self.trace_funcs("started")
            self.get_components().clear()
            sub_end_port = None  # self.outports.get("*SUBEND")
            sub_in_port = None  # self.inports.get("*CONTROL")
            if sub_in_port is not None:
                p = sub_in_port.receive()
                if p is not None:
                    self.drop(p)

            # use fields instead!
            # tracing = mother.tracing
            # trace_file_list = mother.trace_file_list

            self.define()
            # FIXME: warn if any external ports have not been connected

            # if not all(c._check_required_ports() for c in
            #            self.get_components().values()):
            #     raise FlowError(
            #         "One or more mandatory connections have been left unconnected: " + self.get_name())
            self.initiate()
            # activate_all()
            # don't do deadlock testing in subnets - you need to consider
            # the whole net!
            self.deadlock_test = False
            self.wait_for_all()

            for ip in self.inports.ports():
                if ip.is_static():
                    ip.close()

            # Iterator allout = (outports.values()).iterator()
            # while (allout.has_next()):
            # OutputPort op = (OutputPort) allout.next() op.close()

            # status = StatusValues.TERMINATED # will not be set if
            # never activated
            # mother.indicate_terminated(self)
            self.trace_funcs("closed down")
            if sub_end_port is not None:
                sub_end_port.send(Packet(None, self))

    # def open_ports(self):
    #     pass

    def signal_error(self, e):
        if self.status != StatusValues.ERROR:
            self.mother.signal_error(e)
            self.terminate(StatusValues.ERROR)

    def terminate(self, new_status=StatusValues.TERMINATED):
        """
        new_status : StatusValues
        """
        for comp in self.get_components().values():
            comp.terminate(new_status)
        self.status = new_status
        self.interrupt()


@inport("NAME")
@inport("IN")
@outport("OUT")
class SubOI(Component):
    """Look after (synchronous) output/input from/to subnet.
    This component sends a single packet out to the (external) output port,
    and then immediately does a receive from the corresponding (external) input
    port. This process repeats until a None is received on the input port.
    """

    def execute(self):
        np = self.nameport.receive()
        if np is None:
            return
        self.nameport.close()
        pname = np.get_contents()
        self.drop(np)

        i = pname.index_of(":")
        oname = pname.substring(0, i)
        iname = pname.substring(i + 1)
        extoutport = self.mother.get_outports().get(oname)
        self.mother.trace_funcs(
            self.get_name() + ": Accessing output port: " + extoutport.get_name())

        old_sender = extoutport.get_sender()
        extoutport.set_sender(self)

        extinport = self.mother.get_inports().get(iname)
        self.mother.trace_funcs(
            self.get_name() + ": Accessing input port: " + extinport.get_name())
        old_receiver = extinport.get_receiver()
        extinport.set_receiver(self)

        for p in self.inport.receive():
            self.extoutport.send(p)

            p = extinport.receive()
            p.set_owner(self)
            self.outport.send(p)

        self.mother.trace_funcs(
            self.get_name() + ": Releasing input port: " + extinport.get_name())
        extinport.set_receiver(old_receiver)

        self.mother.trace_funcs(
            self.get_name() + ": Releasing output port: " + extoutport.get_name())
        extoutport.set_sender(old_sender)

    def open_ports(self):
        self.nameport = self.open_input("NAME")
        self.inport = self.open_input("IN")
        self.outport = self.open_output("OUT")

# vim:ts=4:sw=4:et:ai:sts=4

import os, weakref
import netns.iproute

__all__ = ['NodeInterface', 'P2PInterface', 'ForeignInterface',
'ForeignNodeInterface', 'ImportedNodeInterface', 'Link']

class Interface(object):
    """Just a base class for the *Interface classes: assign names and handle
    destruction."""
    _nextid = 0
    @staticmethod
    def _gen_next_id():
        n = Interface._nextid
        Interface._nextid += 1
        return n

    @staticmethod
    def _gen_if_name():
        n = Interface._gen_next_id()
        # Max 15 chars
        return "NETNSif-%.4x%.3x" % (os.getpid(), n)

    def __init__(self, index):
        self._idx = index

    def __del__(self):
        self.destroy()

    def destroy(self):
        raise NotImplementedError

    @property
    def index(self):
        """Interface index as seen by the kernel."""
        return self._idx

    @property
    def control(self):
        """Associated interface in the main name space (if it exists). Only
        control interfaces can be put into a Link, for example."""
        return None

class NSInterface(Interface):
    """Add user-facing methods for interfaces that go into a netns."""
    def __init__(self, node, index):
        super(NSInterface, self).__init__(index)
        self._slave = node._slave
        node._add_interface(self)

    # some black magic to automatically get/set interface attributes
    def __getattr__(self, name):
        iface = self._slave.get_if_data(self.index)
        return getattr(iface, name)

    def __setattr__(self, name, value):
        if name[0] == '_': # forbid anything that doesn't start with a _
            super(Interface, self).__setattr__(name, value)
            return
        iface = netns.iproute.interface(index = self.index)
        setattr(iface, name, value)
        return self._slave.set_if(iface)

    def add_v4_address(self, address, prefix_len, broadcast = None):
        addr = netns.iproute.ipv4address(address, prefix_len, broadcast)
        self._slave.add_addr(self.index, addr)

    def add_v6_address(self, address, prefix_len):
        addr = netns.iproute.ipv6address(address, prefix_len)
        self._slave.add_addr(self.index, addr)

    def del_v4_address(self, address, prefix_len, broadcast = None):
        addr = netns.iproute.ipv4address(address, prefix_len, broadcast)
        self._slave.del_addr(self.index, addr)

    def del_v6_address(self, address, prefix_len):
        addr = netns.iproute.ipv6address(address, prefix_len)
        self._slave.del_addr(self.index, addr)

    def get_addresses(self):
        addresses = self._slave.get_addr_data(self.index)
        ret = []
        for a in addresses:
            if hasattr(a, 'broadcast'):
                ret.append(dict(
                    address = a.address,
                    prefix_len = a.prefix_len,
                    broadcast = a.broadcast,
                    family = 'inet'))
            else:
                ret.append(dict(
                    address = a.address,
                    prefix_len = a.prefix_len,
                    family = 'inet6'))
        return ret

class NodeInterface(NSInterface):
    """Class to create and handle a virtual interface inside a name space, it
    can be connected to a Link object with emulation of link
    characteristics."""
    def __init__(self, node):
        """Create a new interface. `node' is the name space in which this
        interface should be put."""
        if1 = netns.iproute.interface(name = self._gen_if_name())
        if2 = netns.iproute.interface(name = self._gen_if_name())
        ctl, ns = netns.iproute.create_if_pair(if1, if2)
        try:
            netns.iproute.change_netns(ns, node.pid)
        except:
            netns.iproute.del_if(ctl)
            # the other interface should go away automatically
            raise
        self._control = SlaveInterface(ctl.index)
        super(NodeInterface, self).__init__(node, ns.index)

    @property
    def control(self):
        return self._control

    def destroy(self):
        if self._slave:
            if self.index in self._slave.get_if_data():
                self._slave.del_if(self.index)
            self._slave = None

class P2PInterface(NSInterface):
    """Class to create and handle point-to-point interfaces between name
    spaces, without using Link objects. Those do not allow any kind of traffic
    shaping.
    As two interfaces need to be created, instead of using the class
    constructor, use the P2PInterface.create_pair() static method."""
    @staticmethod
    def create_pair(node1, node2):
        """Create and return a pair of connected P2PInterface objects, assigned
        to name spaces represented by `node1' and `node2'."""
        if1 = netns.iproute.interface(name = P2PInterface._gen_if_name())
        if2 = netns.iproute.interface(name = P2PInterface._gen_if_name())
        pair = netns.iproute.create_if_pair(if1, if2)
        try:
            netns.iproute.change_netns(pair[0], node1.pid)
            netns.iproute.change_netns(pair[1], node2.pid)
        except:
            netns.iproute.del_if(pair[0])
            # the other interface should go away automatically
            raise

        o1 = P2PInterface.__new__(P2PInterface)
        super(P2PInterface, o1).__init__(node1, pair[0].index)

        o2 = P2PInterface.__new__(P2PInterface)
        super(P2PInterface, o2).__init__(node2, pair[1].index)

        return o1, o2

    def __init__(self):
        "Not to be called directly. Use P2PInterface.create_pair()"
        raise RuntimeError(P2PInterface.__init__.__doc__)

    def destroy(self):
        if self._slave:
            if self.index in self._slave.get_if_data():
                self._slave.del_if(self.index)
            self._slave = None

class ForeignNodeInterface(NSInterface):
    """Class to handle already existing interfaces inside a name space, usually
    just the loopback device, but it can be other user-created interfaces. On
    destruction, the code will try to restore the interface to the state it was
    in before being imported into netns."""
    def __init__(self, node, iface):
        iface = node._slave.get_if_data(iface)
        self._original_state = iface.copy()
        super(ForeignNodeInterface, self).__init__(node, iface.index)

    # FIXME: register somewhere for destruction!
    def destroy(self): # override: restore as much as possible
        if self._slave:
            if self.index in self._slave.get_if_data():
                self._slave.set_if(self._original_state)
            self._slave = None

class ImportedNodeInterface(NSInterface):
    """Class to handle already existing interfaces that are migrated inside a
    name space: real devices, tun devices, etc.  On destruction, the interface
    will be restored to the original name space and will try to restore the
    original state."""
    def __init__(self, node, iface):
        iface = netns.iproute.get_if(iface)
        self._original_state = iface.copy()

        # Change the name to avoid clashes
        iface.name = self._gen_if_name()
        netns.iproute.set_if(iface)

        netns.iproute.change_netns(iface, node.pid)
        super(ImportedNodeInterface, self).__init__(node, iface.index)

    def destroy(self): # override: restore as much as possible
        if self._slave:
            if self.index in self._slave.get_if_data():
                self._slave.change_netns(self.index, os.getpid())
            # else, assume it is in the main name space
            netns.iproute.set_if(self._original_state)
            self._slave = None

class ExternalInterface(Interface):
    """Add user-facing methods for interfaces that run in the main namespace."""
    @property
    def control(self):
        # This is *the* control interface
        return self

    # some black magic to automatically get/set interface attributes
    def __getattr__(self, name):
        iface = netns.iproute.get_if(self.index)
        return getattr(iface, name)

    def __setattr__(self, name, value):
        if name[0] == '_': # forbid anything that doesn't start with a _
            super(ExternalInterface, self).__setattr__(name, value)
            return
        iface = netns.iproute.interface(index = self.index)
        setattr(iface, name, value)
        return netns.iproute.set_if(iface)

    def add_v4_address(self, address, prefix_len, broadcast = None):
        addr = netns.iproute.ipv4address(address, prefix_len, broadcast)
        netns.iproute.add_addr(self.index, addr)

    def add_v6_address(self, address, prefix_len):
        addr = netns.iproute.ipv6address(address, prefix_len)
        netns.iproute.add_addr(self.index, addr)

    def del_v4_address(self, address, prefix_len, broadcast = None):
        addr = netns.iproute.ipv4address(address, prefix_len, broadcast)
        netns.iproute.del_addr(self.index, addr)

    def del_v6_address(self, address, prefix_len):
        addr = netns.iproute.ipv6address(address, prefix_len)
        netns.iproute.del_addr(self.index, addr)

    def get_addresses(self):
        addresses = netns.iproute.get_addr_data(self.index)
        ret = []
        for a in addresses:
            if hasattr(a, 'broadcast'):
                ret.append(dict(
                    address = a.address,
                    prefix_len = a.prefix_len,
                    broadcast = a.broadcast,
                    family = 'inet'))
            else:
                ret.append(dict(
                    address = a.address,
                    prefix_len = a.prefix_len,
                    family = 'inet6'))
        return ret

class SlaveInterface(ExternalInterface):
    """Class to handle the main-name-space-facing couples of Nodeinterface.
    Does nothing, just avoids any destroy code."""
    def destroy(self):
        pass

class ForeignInterface(ExternalInterface):
    """Class to handle already existing interfaces. This kind of interfaces can
    only be connected to Link objects and not assigned to a name space.
    On destruction, the code will try to restore the interface to the state it
    was in before being imported into netns."""
    def __init__(self, iface):
        iface = netns.iproute.get_if(iface)
        self._original_state = iface.copy()
        super(ForeignInterface, self).__init__(iface.index)

    # FIXME: register somewhere for destruction!
    def destroy(self): # override: restore as much as possible
        if self._original_state:
            netns.iproute.set_if(self._original_state)
        self._original_state = None

# Link is just another interface type

class Link(ExternalInterface):
    @staticmethod
    def _gen_br_name():
        n = Link._gen_next_id()
        # Max 15 chars
        return "NETNSbr-%.4x%.3x" % (os.getpid(), n)

    def __init__(self, **args):
        """Creates a new Link object, which models a linux bridge device.
        Parameters are passed to the set_parameters() method after creation."""
        iface = netns.iproute.create_bridge(self._gen_br_name())
        super(Link, self).__init__(iface.index)

        self._parameters = {}
        self._ports = weakref.WeakValueDictionary()
        # FIXME: is this correct/desirable/etc?
        self.stp = False
        self.forward_delay = 0
        # FIXME: register somewhere
        if args:
            self.set_parameters(**args)

    def __getattr__(self, name):
        iface = netns.iproute.get_bridge(self.index)
        return getattr(iface, name)

    def __setattr__(self, name, value):
        if name[0] == '_': # forbid anything that doesn't start with a _
            super(Link, self).__setattr__(name, value)
            return
        # Set ports
        if name in ('up', 'mtu'):
            for i in self._ports.values():
                setattr(i, name, value)
        # Set bridge
        iface = netns.iproute.bridge(index = self.index)
        setattr(iface, name, value)
        netns.iproute.set_bridge(iface)

    def __del__(self):
        self.destroy()

    def destroy(self):
        if not self.index:
            return
        self.up = False
        for p in self._ports.values():
            try:
                self.disconnect(p)
            except:
                pass
        self._ports.clear()
        netns.iproute.del_bridge(self.index)
        self._idx = None

    def connect(self, iface):
        assert iface.control.index not in self._ports
        try:
            self._apply_parameters(self._parameters, iface.control)
            netns.iproute.add_bridge_port(self.index, iface.control.index)
        except:
            self._apply_parameters({}, iface.control)
            raise
        iface.control.up = self.up
        iface.control.mtu = self.mtu
        self._ports[iface.control.index] = iface.control

    def disconnect(self, iface):
        assert iface.control.index in self._ports
        netns.iproute.del_bridge_port(self.index, iface.control.index)
        self._apply_parameters({}, iface.control)
        del self._ports[iface.control.index]

    def set_parameters(self, bandwidth = None,
            delay = None, delay_jitter = None,
            delay_correlation = None, delay_distribution = None,
            loss = None, loss_correlation = None,
            dup = None, dup_correlation = None,
            corrupt = None, corrupt_correlation = None):
        parameters = dict(bandwidth = bandwidth,
                delay = delay, delay_jitter = delay_jitter,
                delay_correlation = delay_correlation,
                delay_distribution = delay_distribution,
                loss = loss, loss_correlation = loss_correlation,
                dup = dup, dup_correlation = dup_correlation,
                corrupt = corrupt, corrupt_correlation = corrupt_correlation)
        try:
            self._apply_parameters(parameters)
        except:
            self._apply_parameters(self._parameters)
            raise
        self._parameters = parameters

    def _apply_parameters(self, parameters, port = None):
        for i in [port] if port else self._ports.values():
            netns.iproute.set_tc(i.index, **parameters)


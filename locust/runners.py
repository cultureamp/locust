# -*- coding: utf-8 -*-
import logging
import random
import socket
import traceback
import warnings
from uuid import uuid4
from time import time

import gevent
import six
from gevent import GreenletExit
from gevent.pool import Group

from six.moves import xrange

from . import events
from .rpc import Message, rpc
from .stats import global_stats

logger = logging.getLogger(__name__)

# global locust runner singleton
locust_runner = None

STATE_INIT, STATE_HATCHING, STATE_RUNNING, STATE_CLEANUP, STATE_STOPPING, STATE_STOPPED, STATE_MISSING = ["ready", "hatching", "running", "cleanup", "stopping", "stopped", "missing"]
SLAVE_REPORT_INTERVAL = 3.0

LOCUST_STATE_RUNNING, LOCUST_STATE_WAITING, LOCUST_STATE_STOPPING = ["running", "waiting", "stopping"]

class LocustRunner(object):
    def __init__(self, locust_classes, options):
        self.options = options
        self.locust_classes = locust_classes
        self.hatch_classes = locust_classes
        self.hatch_rate = options.hatch_rate
        self.num_clients = options.num_clients
        self.host = options.host
        self.locusts = Group()
        self.greenlet = self.locusts
        self.state = STATE_INIT
        self.hatching_greenlet = None
        self.stepload_greenlet = None
        self.exceptions = {}
        self.stats = global_stats
        self.step_load = options.step_load

        # register listener that resets stats when hatching is complete
        def on_hatch_complete(user_count):
            self.state = STATE_RUNNING
            if self.options.reset_stats:
                logger.info("Resetting stats\n")
                self.stats.reset_all()
        events.hatch_complete += on_hatch_complete

    @property
    def request_stats(self):
        return self.stats.entries
    
    @property
    def errors(self):
        return self.stats.errors
    
    @property
    def user_count(self):
        return len(self.locusts)

    def weight_locusts(self, amount):
        """
        Distributes the amount of locusts for each WebLocust-class according to it's weight
        returns a list "bucket" with the weighted locusts
        """
        bucket = []
        weight_sum = sum((locust.weight for locust in self.hatch_classes if locust.task_set))
        residuals = {}
        for locust in self.hatch_classes:
            if not locust.task_set:
                warnings.warn("Notice: Found Locust class (%s) got no task_set. Skipping..." % locust.__name__)
                continue

            if self.host is not None:
                locust.host = self.host

            # create locusts depending on weight
            percent = locust.weight / float(weight_sum)
            num_locusts = int(round(amount * percent))
            bucket.extend([locust for x in xrange(0, num_locusts)])
            # used to keep track of the amount of rounding was done if we need
            # to add/remove some instances from bucket
            residuals[locust] = amount * percent - round(amount * percent)
        if len(bucket) < amount:
            # We got too few locust classes in the bucket, so we need to create a few extra locusts,
            # and we do this by iterating over each of the Locust classes - starting with the one
            # where the residual from the rounding was the largest - and creating one of each until
            # we get the correct amount
            for locust in [l for l, r in sorted(residuals.items(), key=lambda x:x[1], reverse=True)][:amount-len(bucket)]:
                bucket.append(locust)
        elif len(bucket) > amount:
            # We've got too many locusts due to rounding errors so we need to remove some
            for locust in [l for l, r in sorted(residuals.items(), key=lambda x:x[1])][:len(bucket)-amount]:
                bucket.remove(locust)

        return bucket

    def spawn_locusts(self, spawn_count=None, wait=False):
        if spawn_count is None:
            spawn_count = self.num_clients

        bucket = self.weight_locusts(spawn_count)
        spawn_count = len(bucket)
        if self.state == STATE_INIT or self.state == STATE_STOPPED:
            self.state = STATE_HATCHING
            self.num_clients = spawn_count
        else:
            self.num_clients += spawn_count

        logger.info("Hatching and swarming %i clients at the rate %g clients/s..." % (spawn_count, self.hatch_rate))
        occurrence_count = dict([(l.__name__, 0) for l in self.hatch_classes])
        
        def hatch():
            sleep_time = 1.0 / self.hatch_rate
            while True:
                if not bucket:
                    logger.info("All locusts hatched: %s" % ", ".join(["%s: %d" % (name, count) for name, count in six.iteritems(occurrence_count)]))
                    events.hatch_complete.fire(user_count=self.num_clients)
                    return

                locust = bucket.pop(random.randint(0, len(bucket)-1))
                occurrence_count[locust.__name__] += 1
                new_locust = locust()
                def start_locust(_):
                    try:
                        new_locust.run(runner=self)
                    except GreenletExit:
                        pass
                self.locusts.spawn(start_locust, new_locust)
                if len(self.locusts) % 10 == 0:
                    logger.debug("%i locusts hatched" % len(self.locusts))
                gevent.sleep(sleep_time)
        
        hatch()
        if wait:
            self.locusts.join()
            logger.info("All locusts dead\n")

    def kill_locusts(self, kill_count):
        """
        Kill a kill_count of weighted locusts from the Group() object in self.locusts
        """
        bucket = self.weight_locusts(kill_count)
        kill_count = len(bucket)
        self.num_clients -= kill_count
        logger.info("Killing %i locusts" % kill_count)
        dying = []
        for g in self.locusts:
            for l in bucket:
                if l == type(g.args[0]):
                    dying.append(g)
                    bucket.remove(l)
                    break
        self.kill_locust_greenlets(dying)
        events.hatch_complete.fire(user_count=self.num_clients)
    
    def kill_locust_greenlets(self, greenlets):
        """
        Kill running locust greenlets. If options.stop_timeout is set, we try to stop the 
        Locust users gracefully
        """
        if self.options.stop_timeout:
            dying = Group()
            for g in greenlets:
                locust = g.args[0]
                if locust._state == LOCUST_STATE_WAITING:
                    self.locusts.killone(g)
                else:
                    locust._state = LOCUST_STATE_STOPPING
                    dying.add(g)
            if not dying.join(timeout=self.options.stop_timeout):
                logger.info("Not all locusts finished their tasks & terminated in %s seconds. Killing them..." % self.options.stop_timeout)
            dying.kill(block=True)
        else:
            for g in greenlets:
                self.locusts.killone(g)

    def set_hatch_classes(self, class_names):
        """Set the Locust classes to hatch."""
        if class_names:
            self.hatch_classes = [cls for cls in self.locust_classes if cls.__name__ in class_names]

    def start_hatching(self, locust_count=None, hatch_rate=None, wait=False, class_names=None):
        if self.state != STATE_RUNNING and self.state != STATE_HATCHING:
            self.stats.clear_all()
            self.exceptions = {}
            events.locust_start_hatching.fire()

        self.set_hatch_classes(class_names)

        # Dynamically changing the locust count
        if self.state != STATE_INIT and self.state != STATE_STOPPED:
            self.state = STATE_HATCHING
            if self.num_clients > locust_count:
                # Kill some locusts
                kill_count = self.num_clients - locust_count
                self.kill_locusts(kill_count)
            elif self.num_clients < locust_count:
                # Spawn some locusts
                if hatch_rate:
                    self.hatch_rate = hatch_rate
                spawn_count = locust_count - self.num_clients
                self.spawn_locusts(spawn_count=spawn_count)
            else:
                events.hatch_complete.fire(user_count=self.num_clients)
        else:
            if hatch_rate:
                self.hatch_rate = hatch_rate
            if locust_count is not None:
                self.spawn_locusts(locust_count, wait=wait)
            else:
                self.spawn_locusts(wait=wait)

    def start_stepload(self, locust_count, hatch_rate, step_locust_count, step_duration):
        if locust_count < step_locust_count:
          logger.error("Invalid parameters: total locust count of %d is smaller than step locust count of %d" % (locust_count, step_locust_count))
          return
        self.total_clients = locust_count
        self.hatch_rate = hatch_rate
        self.step_clients_growth = step_locust_count
        self.step_duration = step_duration

        if self.stepload_greenlet:
            logger.info("There is an ongoing swarming in Step Load mode, will stop it now.")
            self.greenlet.killone(self.stepload_greenlet)
        logger.info("Start a new swarming in Step Load mode: total locust count of %d, hatch rate of %d, step locust count of %d, step duration of %d " % (locust_count, hatch_rate, step_locust_count, step_duration))
        self.state = STATE_INIT
        self.stepload_greenlet = self.greenlet.spawn(self.stepload_worker)
        self.stepload_greenlet.link_exception(callback=self.noop)

    def stepload_worker(self):
        current_num_clients = 0
        while self.state == STATE_INIT or self.state == STATE_HATCHING or self.state == STATE_RUNNING:
            current_num_clients += self.step_clients_growth
            if current_num_clients > int(self.total_clients):
                logger.info('Step Load is finished.')
                break
            self.start_hatching(current_num_clients, self.hatch_rate)
            logger.info('Step loading: start hatch job of %d locust.' % (current_num_clients))
            gevent.sleep(self.step_duration)

    def stop(self):
        # if we are currently hatching locusts we need to kill the hatching greenlet first
        if self.hatching_greenlet and not self.hatching_greenlet.ready():
            self.hatching_greenlet.kill(block=True)
        self.kill_locust_greenlets([g for g in self.locusts])
        self.state = STATE_STOPPED
        events.locust_stop_hatching.fire()
    
    def quit(self):
        self.stop()
        self.greenlet.kill(block=True)

    def log_exception(self, node_id, msg, formatted_tb):
        key = hash(formatted_tb)
        row = self.exceptions.setdefault(key, {"count": 0, "msg": msg, "traceback": formatted_tb, "nodes": set()})
        row["count"] += 1
        row["nodes"].add(node_id)
        self.exceptions[key] = row

    def noop(self, *args, **kwargs):
        """ Used to link() greenlets to in order to be compatible with gevent 1.0 """
        pass

class LocalLocustRunner(LocustRunner):
    def __init__(self, locust_classes, options):
        super(LocalLocustRunner, self).__init__(locust_classes, options)

        # register listener thats logs the exception for the local runner
        def on_locust_error(locust_instance, exception, tb):
            formatted_tb = "".join(traceback.format_tb(tb))
            self.log_exception("local", str(exception), formatted_tb)
        events.locust_error += on_locust_error

    def start_hatching(self, locust_count=None, hatch_rate=None, wait=False, class_names=None):
        self.hatching_greenlet = gevent.spawn(lambda: super(LocalLocustRunner, self).start_hatching(locust_count, hatch_rate, wait=wait, class_names=class_names))
        self.greenlet = self.hatching_greenlet

class DistributedLocustRunner(LocustRunner):
    def __init__(self, locust_classes, options):
        super(DistributedLocustRunner, self).__init__(locust_classes, options)
        self.master_host = options.master_host
        self.master_port = options.master_port
        self.master_bind_host = options.master_bind_host
        self.master_bind_port = options.master_bind_port
        self.heartbeat_liveness = options.heartbeat_liveness
        self.heartbeat_interval = options.heartbeat_interval

class SlaveNode(object):
    def __init__(self, id, state=STATE_INIT, heartbeat_liveness=3):
        self.id = id
        self.state = state
        self.user_count = 0
        self.heartbeat = heartbeat_liveness

class MasterLocustRunner(DistributedLocustRunner):
    def __init__(self, *args, **kwargs):
        super(MasterLocustRunner, self).__init__(*args, **kwargs)

        class SlaveNodesDict(dict):
            def get_by_state(self, state):
                return [c for c in six.itervalues(self) if c.state == state]
            
            @property
            def all(self):
                return six.itervalues(self)

            @property
            def ready(self):
                return self.get_by_state(STATE_INIT)
            
            @property
            def hatching(self):
                return self.get_by_state(STATE_HATCHING)
            
            @property
            def running(self):
                return self.get_by_state(STATE_RUNNING)
        
        self.clients = SlaveNodesDict()
        self.server = rpc.Server(self.master_bind_host, self.master_bind_port)
        self.greenlet = Group()
        self.greenlet.spawn(self.heartbeat_worker).link_exception(callback=self.noop)
        self.greenlet.spawn(self.client_listener).link_exception(callback=self.noop)

        # listener that gathers info on how many locust users the slaves has spawned
        def on_slave_report(client_id, data):
            if client_id not in self.clients:
                logger.info("Discarded report from unrecognized slave %s", client_id)
                return

            self.clients[client_id].user_count = data["user_count"]
        events.slave_report += on_slave_report
        
        # register listener that sends quit message to slave nodes
        def on_quitting():
            self.quit()
        events.quitting += on_quitting
    
    @property
    def user_count(self):
        return sum([c.user_count for c in six.itervalues(self.clients)])
    
    def start_hatching(self, locust_count=None, hatch_rate=None, wait=False, class_names=None):
        num_slaves = len(self.clients.ready) + len(self.clients.running) + len(self.clients.hatching)
        if not num_slaves:
            logger.warning("You are running in distributed mode but have no slave servers connected. "
                           "Please connect slaves prior to swarming.")
            return

        self.num_clients = locust_count
        self.hatch_rate = hatch_rate
        slave_num_clients = locust_count // (num_slaves or 1)
        slave_hatch_rate = float(hatch_rate) / (num_slaves or 1)
        remaining = locust_count % num_slaves

        self.set_hatch_classes(class_names)

        logger.info("Sending hatch jobs of %d locusts and %.2f hatch rate to %d ready clients" % (slave_num_clients, slave_hatch_rate, num_slaves))

        if self.state != STATE_RUNNING and self.state != STATE_HATCHING:
            self.stats.clear_all()
            self.exceptions = {}
            events.master_start_hatching.fire()
        
        for client in (self.clients.ready + self.clients.running + self.clients.hatching):
            data = {
                "hatch_rate": slave_hatch_rate,
                "num_clients": slave_num_clients,
                "host": self.host,
                "stop_timeout": self.options.stop_timeout,
                "class_names": [cls.__name__ for cls in self.hatch_classes],
            }

            if remaining > 0:
                data["num_clients"] += 1
                remaining -= 1

            self.server.send_to_client(Message("hatch", data, client.id))
        
        self.state = STATE_HATCHING

    def stop(self):
        self.state = STATE_STOPPING
        for client in self.clients.all:
            self.server.send_to_client(Message("stop", None, client.id))
        events.master_stop_hatching.fire()
    
    def quit(self):
        for client in self.clients.all:
            self.server.send_to_client(Message("quit", None, client.id))
        gevent.sleep(0.5) # wait for final stats report from all slaves
        self.greenlet.kill(block=True)
    
    def heartbeat_worker(self):
        while True:
            gevent.sleep(self.heartbeat_interval)
            for client in self.clients.all:
                if client.heartbeat < 0 and client.state != STATE_MISSING:
                    logger.info('Slave %s failed to send heartbeat, setting state to missing.' % str(client.id))
                    client.state = STATE_MISSING
                    client.user_count = 0
                else:
                    client.heartbeat -= 1

    def client_listener(self):
        while True:
            client_id, msg = self.server.recv_from_client()
            msg.node_id = client_id
            if msg.type == "client_ready":
                id = msg.node_id
                self.clients[id] = SlaveNode(id, heartbeat_liveness=self.heartbeat_liveness)
                logger.info("Client %r reported as ready. Currently %i clients ready to swarm." % (id, len(self.clients.ready + self.clients.running + self.clients.hatching)))
                # balance the load distribution when new client joins
                if self.state == STATE_RUNNING or self.state == STATE_HATCHING:
                    self.start_hatching(self.num_clients, self.hatch_rate)
                ## emit a warning if the slave's clock seem to be out of sync with our clock
                #if abs(time() - msg.data["time"]) > 5.0:
                #    warnings.warn("The slave node's clock seem to be out of sync. For the statistics to be correct the different locust servers need to have synchronized clocks.")
            elif msg.type == "client_stopped":
                del self.clients[msg.node_id]
                logger.info("Removing %s client from running clients" % (msg.node_id))
            elif msg.type == "heartbeat":
                if msg.node_id in self.clients:
                    self.clients[msg.node_id].heartbeat = self.heartbeat_liveness
                    self.clients[msg.node_id].state = msg.data['state']
            elif msg.type == "stats":
                events.slave_report.fire(client_id=msg.node_id, data=msg.data)
            elif msg.type == "hatching":
                self.clients[msg.node_id].state = STATE_HATCHING
            elif msg.type == "hatch_complete":
                self.clients[msg.node_id].state = STATE_RUNNING
                self.clients[msg.node_id].user_count = msg.data["count"]
                if len(self.clients.hatching) == 0:
                    count = sum(c.user_count for c in six.itervalues(self.clients))
                    events.hatch_complete.fire(user_count=count)
            elif msg.type == "quit":
                if msg.node_id in self.clients:
                    del self.clients[msg.node_id]
                    logger.info("Client %r quit. Currently %i clients connected." % (msg.node_id, len(self.clients.ready)))
            elif msg.type == "exception":
                self.log_exception(msg.node_id, msg.data["msg"], msg.data["traceback"])

            if not self.state == STATE_INIT and all(map(lambda x: x.state != STATE_RUNNING and x.state != STATE_HATCHING, self.clients.all)):
                self.state = STATE_STOPPED

    @property
    def slave_count(self):
        return len(self.clients.ready) + len(self.clients.hatching) + len(self.clients.running)

class SlaveLocustRunner(DistributedLocustRunner):
    def __init__(self, *args, **kwargs):
        super(SlaveLocustRunner, self).__init__(*args, **kwargs)
        self.client_id = socket.gethostname() + "_" + uuid4().hex
        
        self.client = rpc.Client(self.master_host, self.master_port, self.client_id)
        self.greenlet = Group()

        self.greenlet.spawn(self.heartbeat).link_exception(callback=self.noop)
        self.greenlet.spawn(self.worker).link_exception(callback=self.noop)
        self.client.send(Message("client_ready", None, self.client_id))
        self.slave_state = STATE_INIT
        self.greenlet.spawn(self.stats_reporter).link_exception(callback=self.noop)
        
        # register listener for when all locust users have hatched, and report it to the master node
        def on_hatch_complete(user_count):
            self.client.send(Message("hatch_complete", {"count":user_count}, self.client_id))
            self.slave_state = STATE_RUNNING
        events.hatch_complete += on_hatch_complete
        
        # register listener that adds the current number of spawned locusts to the report that is sent to the master node 
        def on_report_to_master(client_id, data):
            data["user_count"] = self.user_count
        events.report_to_master += on_report_to_master
        
        # register listener that sends quit message to master
        def on_quitting():
            self.client.send(Message("quit", None, self.client_id))
        events.quitting += on_quitting

        # register listener thats sends locust exceptions to master
        def on_locust_error(locust_instance, exception, tb):
            formatted_tb = "".join(traceback.format_tb(tb))
            self.client.send(Message("exception", {"msg" : str(exception), "traceback" : formatted_tb}, self.client_id))
        events.locust_error += on_locust_error

    def heartbeat(self):
        while True:
            self.client.send(Message('heartbeat', {'state': self.slave_state}, self.client_id))
            gevent.sleep(self.heartbeat_interval)

    def worker(self):
        while True:
            msg = self.client.recv()
            if msg.type == "hatch":
                self.slave_state = STATE_HATCHING
                self.client.send(Message("hatching", None, self.client_id))
                job = msg.data
                self.set_hatch_classes(job["class_names"])
                self.hatch_rate = job["hatch_rate"]
                #self.num_clients = job["num_clients"]
                self.host = job["host"]
                self.options.stop_timeout = job["stop_timeout"]
                self.hatching_greenlet = gevent.spawn(lambda: self.start_hatching(locust_count=job["num_clients"],
                                                                                  hatch_rate=job["hatch_rate"],
                                                                                  class_names=job["class_names"]))
            elif msg.type == "stop":
                self.stop()
                self.client.send(Message("client_stopped", None, self.client_id))
                self.client.send(Message("client_ready", None, self.client_id))
                self.slave_state = STATE_INIT
            elif msg.type == "quit":
                logger.info("Got quit message from master, shutting down...")
                self.stop()
                self._send_stats() # send a final report, in case there were any samples not yet reported
                self.greenlet.kill(block=True)

    def stats_reporter(self):
        while True:
            try:
                self._send_stats()
            except:
                logger.error("Connection lost to master server. Aborting...")
                break
            
            gevent.sleep(SLAVE_REPORT_INTERVAL)

    def _send_stats(self):
        data = {}
        events.report_to_master.fire(client_id=self.client_id, data=data)
        self.client.send(Message("stats", data, self.client_id))

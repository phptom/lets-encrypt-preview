"""ACME protocol client class and helper functions."""
import collections
import csv
import json
import logging
import os
import shutil
import socket
import string
import sys
import time

import jsonschema
import M2Crypto
import requests

from letsencrypt.client import acme
from letsencrypt.client import apache_configurator
from letsencrypt.client import challenge
from letsencrypt.client import CONFIG
from letsencrypt.client import crypto_util
from letsencrypt.client import display
from letsencrypt.client import errors
from letsencrypt.client import le_util


# it's weird to point to chocolate servers via raw IPv6 addresses, and
# such addresses can be %SCARY in some contexts, so out of paranoia
# let's disable them by default
ALLOW_RAW_IPV6_SERVER = False


class Client(object):
    """ACME protocol client.

    :ivar config: Configurator.
    :type config: :class:`letsencrypt.client.configurator.Configurator`

    :ivar str server: Certificate authority server
    :ivar str server_url: Full URL of the CSR server

    :ivar csr: Certificate Signing Request
    :type csr: :class:`CSR`

    :ivar list names: Domain names (:class:`list` of :class:`str`).

    :ivar privkey: Private key
    :type privkey: :class:`Key`

    :ivar bool use_curses: Use curses UI

    """
    Key = collections.namedtuple("Key", "file pem")
    CSR = collections.namedtuple("CSR", "file data type")

    def __init__(self, server, csr=CSR(None, None, None),
                 privkey=Key(None, None), use_curses=True):
        """Initialize a client."""
        self.server = server
        self.server_url = "https://%s/acme/" % self.server
        self.names = []
        self.use_curses = use_curses

        self.csr = csr
        self.privkey = privkey
        self._validate_csr_key_cli()  # TODO: catch exceptions

        # TODO: Can probably figure out which configurator to use
        #       without special packaging based on system info Command
        #       line arg or client function to discover
        self.config = apache_configurator.ApacheConfigurator(
            CONFIG.SERVER_ROOT)

    def authenticate(self, domains=None, eula=False, redirect=None):
        """

        :param list domains: List of domains
        :param bool eula: EULA accepted

        :param redirect: If traffic should be forwarded from HTTP to HTTPS.
        :type redirect: bool or None

        :raises errors.LetsEncryptClientError: CSR does not contain one of the
            specified names.

        """
        domains = [] if domains is None else domains

        # Check configuration
        if not self.config.config_test():
            sys.exit(1)

        # Display preview warning
        if not eula:
            with open('EULA') as eula_file:
                if not display.generic_yesno(eula_file.read(),
                                             "Agree", "Cancel"):
                    sys.exit(0)

        # Display screen to select domains to validate
        if domains:
            sanity_check_names([self.server] + domains)
            self.names = domains
        else:
            # This function adds all names
            # found within the config to self.names
            # Then filters them based on user selection
            code, self.names = display.filter_names(self.get_all_names())
            if code == display.OK and self.names:
                # TODO: Allow multiple names once it is setup
                self.names = [self.names[0]]
            else:
                sys.exit(0)

        # Request Challenges
        challenge_msg = self.acme_challenge()

        # Make sure we have key and csr to perform challenges
        self.init_key_csr()

        # Perform Challenges
        responses, challenge_objs = self.verify_identity(challenge_msg)
        # Get Authorization
        self.acme_authorization(challenge_msg, challenge_objs, responses)

        # Retrieve certificate
        certificate_dict = self.acme_certificate(self.csr.data)

        # Find set of virtual hosts to deploy certificates to
        vhost = self.get_virtual_hosts(self.names)

        # Install Certificate
        cert_file = self.install_certificate(certificate_dict, vhost)

        # Perform optimal config changes
        self.optimize_config(vhost, redirect)

        self.config.save("Completed Let's Encrypt Authentication")

        self.store_cert_key(cert_file, False)

    def acme_challenge(self):
        """Handle ACME "challenge" phase.

        .. todo:: Handle more than one domain name in self.names

        :returns: ACME "challenge" message.
        :rtype: dict

        """
        return self.send_and_receive_expected(
            acme.challenge_request(self.names[0]), "challenge")

    def acme_authorization(self, challenge_msg, chal_objs, responses):
        """Handle ACME "authorization" phase.

        :param dict challenge_msg: ACME "challenge" message.

        :param chal_objs: TODO
        :param responses: TODO

        :returns: ACME "authorization" message.
        :rtype: dict

        """
        auth_dict = self.send(acme.authorization_request(
            challenge_msg["sessionID"], self.names[0],
            challenge_msg["nonce"], responses, self.privkey.pem))

        try:
            return self.is_expected_msg(auth_dict, "authorization")
        except:
            logging.fatal(
                "Failed Authorization procedure - cleaning up challenges")
            sys.exit(1)
        finally:
            self.cleanup_challenges(chal_objs)

    def acme_certificate(self, csr_der):
        """Handle ACME "certificate" phase.

        :param str csr_der: CSR in DER format.

        :returns: ACME "certificate" message.
        :rtype: dict

        """
        logging.info("Preparing and sending CSR...")
        return self.send_and_receive_expected(
            acme.certificate_request(csr_der, self.privkey.pem), "certificate")

    def acme_revocation(self, cert):
        """Handle ACME "revocation" phase.

        :param dict cert: TODO

        :returns: ACME "revocation" message.
        :rtype: dict

        """
        cert_der = M2Crypto.X509.load_cert(cert["backup_cert_file"]).as_der()
        with open(cert["backup_key_file"], 'rU') as backup_key_file:
            key = backup_key_file.read()

        revocation = self.send_and_receive_expected(
            acme.revocation_request(cert_der, key), "revocation")

        display.generic_notification(
            "You have successfully revoked the certificate for "
            "%s" % cert["cn"], width=70, height=9)

        remove_cert_key(cert)
        self.list_certs_keys()

        return revocation

    def send(self, msg):
        """Send ACME message to server.

        :param dict msg: ACME message (JSON serializable).

        :returns: Server response message.
        :rtype: dict

        :raises TypeError: if `msg` is not JSON serializable
        :raises jsonschema.ValidationError: if not valid ACME message
        :raises errors.LetsEncryptClientError: in case of connection error
            or if response from server is not a valid ACME message.

        """
        json_encoded = json.dumps(msg)
        acme.acme_object_validate(json_encoded)

        try:
            response = requests.post(
                self.server_url,
                data=json_encoded,
                headers={"Content-Type": "application/json"},
            )
        except requests.exceptions.RequestException as error:
            raise errors.LetsEncryptClientError(
                'Sending ACME message to server has failed: %s' % error)

        try:
            acme.acme_object_validate(response.content)
        except ValueError:
            raise errors.LetsEncryptClientError(
                'Server did not send JSON serializable message')
        except jsonschema.ValidationError as error:
            raise errors.LetsEncryptClientError(
                'Response from server is not a valid ACME message')

        return response.json()

    def send_and_receive_expected(self, msg, expected):
        """Send ACME message to server and return expected message.

        :param dict msg: ACME message (JSON serializable).
        :param str expected: Name of the expected response ACME message type.

        :returns: ACME response message of expected type.
        :rtype: dict

        :raises errors.LetsEncryptClientError: An exception is thrown

        """
        response = self.send(msg)
        try:
            return self.is_expected_msg(response, expected)
        except:  # TODO: too generic exception
            raise errors.LetsEncryptClientError(
                'Expected message (%s) not received' % expected)

    def is_expected_msg(self, response, expected, delay=3, rounds=20):
        """Is reponse expected ACME message?

        :param dict response: ACME response message from server.

        :param str expected: Name of the expected response ACME message type.

        :param int delay: Number of seconds to delay before next round
            in case of ACME "defer" response message.

        :param int rounds: Number of resend attempts in case of ACME "defer"
            reponse message.

        :returns: ACME response message from server.
        :rtype: dict

        :raises LetsEncryptClientError: if server sent ACME "error" message

        """
        for _ in xrange(rounds):
            if response["type"] == expected:
                return response

            elif response["type"] == "error":
                logging.error(
                    "%s: %s - More Info: %s", response["error"],
                    response.get("message", ""), response.get("moreInfo", ""))
                raise errors.LetsEncryptClientError(response["error"])

            elif response["type"] == "defer":
                logging.info("Waiting for %d seconds...", delay)
                time.sleep(delay)
                response = self.send(acme.status_request(response["token"]))
            else:
                logging.fatal("Received unexpected message")
                logging.fatal("Expected: %s" % expected)
                logging.fatal("Received: " + response)
                sys.exit(33)

        logging.error(
            "Server has deferred past the max of %d seconds", rounds * delay)

    def list_certs_keys(self):
        """List trusted Let's Encrypt certificates."""
        list_file = os.path.join(CONFIG.CERT_KEY_BACKUP, "LIST")
        certs = []

        if not os.path.isfile(list_file):
            logging.info(
                "You don't have any certificates saved from letsencrypt")
            return

        c_sha1_vh = {}
        for (cert, _, path) in self.config.get_all_certs_keys():
            try:
                c_sha1_vh[M2Crypto.X509.load_cert(
                    cert).get_fingerprint(md='sha1')] = path
            except:
                continue

        with open(list_file, 'rb') as csvfile:
            csvreader = csv.reader(csvfile)
            for row in csvreader:
                cert = crypto_util.get_cert_info(row[1])

                b_k = os.path.join(CONFIG.CERT_KEY_BACKUP,
                                   os.path.basename(row[2]) + "_" + row[0])
                b_c = os.path.join(CONFIG.CERT_KEY_BACKUP,
                                   os.path.basename(row[1]) + "_" + row[0])

                cert.update({
                    "orig_key_file": row[2],
                    "orig_cert_file": row[1],
                    "idx": int(row[0]),
                    "backup_key_file": b_k,
                    "backup_cert_file": b_c,
                    "installed": c_sha1_vh.get(cert["fingerprint"], ""),
                })
                certs.append(cert)
        if certs:
            self.choose_certs(certs)
        else:
            display.generic_notification(
                "There are not any trusted Let's Encrypt "
                "certificates for this server.")

    def choose_certs(self, certs):
        """Display choose certificates menu.

        :param list certs: List of cert dicts.

        """
        code, tag = display.display_certs(certs)
        
        if code == display.OK:
            cert = certs[tag]
            if display.confirm_revocation(cert):
                self.acme_revocation(cert)
            else:
                self.choose_certs(certs)
        elif code == display.HELP:
            cert = certs[tag]
            display.more_info_cert(cert)
            self.choose_certs(certs)
        else:
            exit(0)

    def install_certificate(self, certificate_dict, vhost):
        """Install certificate

        :returns: Path to a certificate file.
        :rtype: str

        """
        cert_chain_abspath = None
        cert_fd, cert_file = le_util.unique_file(CONFIG.CERT_PATH, 0o644)
        cert_fd.write(
            crypto_util.b64_cert_to_pem(certificate_dict["certificate"]))
        cert_fd.close()
        logging.info(
            "Server issued certificate; certificate written to %s", cert_file)

        if certificate_dict.get("chain", None):
            chain_fd, chain_fn = le_util.unique_file(CONFIG.CHAIN_PATH, 0o644)
            for cert in certificate_dict.get("chain", []):
                chain_fd.write(crypto_util.b64_cert_to_pem(cert))
            chain_fd.close()

            logging.info("Cert chain written to %s", chain_fn)

            # This expects a valid chain file
            cert_chain_abspath = os.path.abspath(chain_fn)

        for host in vhost:
            self.config.deploy_cert(host,
                                    os.path.abspath(cert_file),
                                    os.path.abspath(self.privkey.file),
                                    cert_chain_abspath)
            # Enable any vhost that was issued to, but not enabled
            if not host.enabled:
                logging.info("Enabling Site %s", host.filep)
                self.config.enable_site(host)

        # sites may have been enabled / final cleanup
        self.config.restart(quiet=self.use_curses)

        display.success_installation(self.names)

        return cert_file

    def optimize_config(self, vhost, redirect=None):
        """Optimize the configuration.

        :param vhost: vhost to optimize
        :type vhost: :class:`apache_configurator.VH`

        :param redirect: If traffic should be forwarded from HTTP to HTTPS.
        :type redirect: bool or None

        """
        # TODO: this should most definitely be moved to __init__
        if redirect is None:
            redirect = display.redirect_by_default()

        if redirect:
            self.redirect_to_ssl(vhost)
            self.config.restart(quiet=self.use_curses)

        # if self.ocsp_stapling is None:
        #     q = ("Would you like to protect the privacy of your users "
        #         "by enabling OCSP stapling? If so, your users will not have "
        #         "to query the Let's Encrypt CA separately about the current "
        #         "revocation status of your certificate.")
        #    self.ocsp_stapling = self.ocsp_stapling = display.ocsp_stapling(q)
        # if self.ocsp_stapling:
        #    # TODO enable OCSP Stapling
        #    continue

    def cleanup_challenges(self, challenges):
        """Cleanup configuration challenges

        :param dict challenges: challenges from a challenge message

        """
        logging.info("Cleaning up challenges...")
        for chall in challenges:
            if chall["type"] in CONFIG.CONFIG_CHALLENGES:
                self.config.cleanup()
            else:
                # Handle other cleanup if needed
                pass

    def verify_identity(self, challenge_msg):
        """Verify identity.

        :param dict challenge_msg: ACME "challenge" message.

        :returns: TODO
        :rtype: dict

        """
        path = challenge.gen_challenge_path(
            challenge_msg["challenges"], challenge_msg.get("combinations", []))

        logging.info("Performing the following challenges:")

        # Every indices element is a list of integers referring to which
        # challenges in the master list the challenge object satisfies
        # Single Challenge objects that can satisfy multiple server challenges
        # mess up the order of the challenges, thus requiring the indices
        challenge_objs, indices = self.challenge_factory(
            self.names[0], challenge_msg["challenges"], path)

        responses = ["null"] * len(challenge_msg["challenges"])

        # Perform challenges
        for i, c_obj in enumerate(challenge_objs):
            resp = "null"
            if c_obj["type"] in CONFIG.CONFIG_CHALLENGES:
                resp = self.config.perform(c_obj)
            else:
                # Handle RecoveryToken type challenges
                pass
            
            self._assign_responses(resp, indices[i], responses)

        logging.info(
            "Configured Apache for challenges; waiting for verification...")

        return responses, challenge_objs

    def _assign_responses(self, resp, index_list, responses):
        """Assign chall_response to appropriate places in response list.

        :param resp: responses from a challenge
        :type resp: list of dicts or dict

        :param list index_list: respective challenges resp satisfies
        :param list responses: master list of responses

        """
        if isinstance(resp, list):
            assert(len(resp) == len(index_list))
            for j, index in enumerate(index_list):
                responses[index] = resp[j]
        else:        
            for index in index_list:
                responses[index] = resp


    def store_cert_key(self, cert_file, encrypt=False):
        """Store certificate key.

        :param str cert_file: Path to a certificate file.

        :param bool encrypt: Should the certificate key be encrypted?

        :returns: True if key file was stored successfully, False otherwise.
        :rtype: bool

        """
        list_file = os.path.join(CONFIG.CERT_KEY_BACKUP, "LIST")
        le_util.make_or_verify_dir(CONFIG.CERT_KEY_BACKUP, 0o700)
        idx = 0

        if encrypt:
            logging.error(
                "Unfortunately securely storing the certificates/"
                "keys is not yet available. Stay tuned for the "
                "next update!")
            return False

        if os.path.isfile(list_file):
            with open(list_file, 'r+b') as csvfile:
                csvreader = csv.reader(csvfile)
                for row in csvreader:
                    idx = int(row[0]) + 1
                csvwriter = csv.writer(csvfile)
                csvwriter.writerow([str(idx), cert_file, self.privkey.file])

        else:
            with open(list_file, 'wb') as csvfile:
                csvwriter = csv.writer(csvfile)
                csvwriter.writerow(["0", cert_file, self.privkey.file])

        shutil.copy2(self.privkey.file,
                     os.path.join(
                         CONFIG.CERT_KEY_BACKUP,
                         os.path.basename(self.privkey.file) + "_" + str(idx)))
        shutil.copy2(cert_file,
                     os.path.join(
                         CONFIG.CERT_KEY_BACKUP,
                         os.path.basename(cert_file) + "_" + str(idx)))

        return True

    def redirect_to_ssl(self, vhost):
        """Redirect all traffic from HTTP to HTTPS

        :param vhost: list of ssl_vhosts
        :type vhost: :class:`apache_configurator.VH`

        """
        for ssl_vh in vhost:
            success, redirect_vhost = self.config.enable_redirect(ssl_vh)
            logging.info(
                "\nRedirect vhost: %s - %s ", redirect_vhost.filep, success)
            # If successful, make sure redirect site is enabled
            if success:
                self.config.enable_site(redirect_vhost)

    def get_virtual_hosts(self, domains):
        """Retrieve the appropriate virtual host for the domain

        :param list domains: Domains to find ssl vhosts for

        :returns: associated vhosts
        :rtype: :class:`apache_configurator.VH`

        """
        vhost = set()
        for name in domains:
            host = self.config.choose_virtual_host(name)
            if host is not None:
                vhost.add(host)
        return vhost

    def challenge_factory(self, name, challenges, path):
        """

        :param name: TODO

        :param list challenges: A list of challenges from ACME "challenge"
            server message to be fulfilled by the client in order to prove
            possession of the identifier.

        :param list path: List of indices from `challenges`.

        :returns: A pair of TODO
        :rtype: tuple

        """
        sni_todo = []
        # Since a single invocation of SNI challenge can satisfy multiple
        # challenges. We must keep track of all the challenges it satisfies
        sni_satisfies = []

        challenge_objs = []
        challenge_obj_indices = []
        for index in path:
            chall = challenges[index]

            if chall["type"] == "dvsni":
                logging.info("  DVSNI challenge for name %s.", name)
                sni_satisfies.append(index)
                sni_todo.append((str(name), str(chall["r"]),
                                 str(chall["nonce"])))

            elif chall["type"] == "recoveryToken":
                logging.info("\tRecovery Token Challenge for name: %s.", name)
                challenge_obj_indices.append(index)
                challenge_objs.append({
                    type: "recoveryToken",
                })

            else:
                logging.fatal("Challenge not currently supported")
                sys.exit(82)

        if sni_todo:
            # SNI_Challenge can satisfy many sni challenges at once so only
            # one "challenge object" is issued for all sni_challenges
            challenge_objs.append({
                "type": "dvsni",
                "list_sni_tuple": sni_todo,
                "dvsni_key": self.privkey,
            })
            challenge_obj_indices.append(sni_satisfies)
            logging.debug(sni_todo)

        return challenge_objs, challenge_obj_indices

    def init_key_csr(self):
        """Initializes privkey and csr.

        Inits key and CSR using provided files or generating new files
        if necessary. Both will be saved in PEM format on the
        filesystem. The CSR is placed into DER format to allow
        the namedtuple to easily work with the protocol.

        """
        if not self.privkey.file:
            key_pem = crypto_util.make_key(CONFIG.RSA_KEY_SIZE)

            # Save file
            le_util.make_or_verify_dir(CONFIG.KEY_DIR, 0o700)
            key_f, key_filename = le_util.unique_file(
                os.path.join(CONFIG.KEY_DIR, "key-letsencrypt.pem"), 0o600)
            key_f.write(key_pem)
            key_f.close()

            logging.info("Generating key: %s", key_filename)

            self.privkey = Client.Key(key_filename, key_pem)

        if not self.csr.file:
            csr_pem, csr_der = crypto_util.make_csr(
                self.privkey.pem, self.names)

            # Save CSR
            le_util.make_or_verify_dir(CONFIG.CERT_DIR, 0o755)
            csr_f, csr_filename = le_util.unique_file(
                os.path.join(CONFIG.CERT_DIR, "csr-letsencrypt.pem"), 0o644)
            csr_f.write(csr_pem)
            csr_f.close()

            logging.info("Creating CSR: %s", csr_filename)

            self.csr = Client.CSR(csr_filename, csr_der, "der")
        elif self.csr.type != "der":
            # The user is going to pass in a pem format file
            # That is why we must conver it to der since the
            # protocol uses der exclusively.
            csr_obj = M2Crypto.X509.load_request_string(self.csr.data)
            self.csr = Client.CSR(self.csr.file, csr_obj.as_der(), "der")

    def _validate_csr_key_cli(self):
        """Validate CSR and key files.

        Verifies that the client key and csr arguments are valid and
        correspond to one another.

        :raises LetsEncryptClientError: if validation fails

        """
        # TODO: Handle all of these problems appropriately
        # The client can eventually do things like prompt the user
        # and allow the user to take more appropriate actions

        # If CSR is provided, it must be readable and valid.
        if self.csr.data and not crypto_util.valid_csr(self.csr.data):
            raise errors.LetsEncryptClientError(
                "The provided CSR is not a valid CSR")

        # If key is provided, it must be readable and valid.
        if (self.privkey.pem and
                not crypto_util.valid_privkey(self.privkey.pem)):
            raise errors.LetsEncryptClientError(
                "The provided key is not a valid key")

        # If CSR and key are provided, the key must be the same key used
        # in the CSR.
        if self.csr.data and self.privkey.pem:
            if not crypto_util.csr_matches_pubkey(
                    self.csr.data, self.privkey.pem):
                raise errors.LetsEncryptClientError(
                    "The key and CSR do not match")

    def get_all_names(self):
        """Return all valid names in the configuration."""
        names = list(self.config.get_all_names())
        sanity_check_names(names)

        if not names:
            logging.fatal("No domain names were found in your apache config")
            logging.fatal("Either specify which names you would like "
                          "letsencrypt to validate or add server names "
                          "to your virtual hosts")
            sys.exit(1)

        return names


def remove_cert_key(cert):
    """Remove certificate key.

    :param dict cert:

    """
    list_file = os.path.join(CONFIG.CERT_KEY_BACKUP, "LIST")
    list_file2 = os.path.join(CONFIG.CERT_KEY_BACKUP, "LIST.tmp")

    with open(list_file, 'rb') as orgfile:
        csvreader = csv.reader(orgfile)

        with open(list_file2, 'wb') as newfile:
            csvwriter = csv.writer(newfile)

            for row in csvreader:
                if not (row[0] == str(cert["idx"]) and
                        row[1] == cert["orig_cert_file"] and
                        row[2] == cert["orig_key_file"]):
                    csvwriter.writerow(row)

    shutil.copy2(list_file2, list_file)
    os.remove(list_file2)
    os.remove(cert["backup_cert_file"])
    os.remove(cert["backup_key_file"])


def sanity_check_names(names):
    """Make sure host names are valid.

    :param list names: List of host names

    """
    for name in names:
        if not is_hostname_sane(name):
            logging.fatal("%r is an impossible hostname", name)
            sys.exit(81)


def is_hostname_sane(hostname):
    """Make sure the given host name is sane.

    Do enough to avoid shellcode from the environment.  There's
    no need to do more.

    :param str hostname: Host name to validate

    :returns: True if hostname is valid, otherwise false.
    :rtype: bool

    """
    # hostnames & IPv4
    allowed = string.ascii_letters + string.digits + "-."
    if all([c in allowed for c in hostname]):
        return True

    if not ALLOW_RAW_IPV6_SERVER:
        return False

    # ipv6 is messy and complicated, can contain %zoneindex etc.
    try:
        # is this a valid IPv6 address?
        socket.getaddrinfo(hostname, 443, socket.AF_INET6)
        return True
    except:
        return False

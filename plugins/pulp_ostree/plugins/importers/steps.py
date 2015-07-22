import os
import errno

from gettext import gettext as _
from logging import getLogger

from gnupg import GPG

from pulp.common.plugins import importer_constants
from pulp.plugins.util.publish_step import PluginStep
from pulp.plugins.model import Unit
from pulp.plugins.util.misc import mkdir
from pulp.server.exceptions import PulpCodedException

from pulp_ostree.common import constants, errors
from pulp_ostree.common import model
from pulp_ostree.plugins import lib


log = getLogger(__name__)


class Main(PluginStep):
    """
    The main synchronization step.
    """

    def __init__(self, **kwargs):
        super(Main, self).__init__(
            step_type=constants.IMPORT_STEP_MAIN,
            plugin_type=constants.WEB_IMPORTER_TYPE_ID,
            **kwargs)
        self.feed_url = self.config.get(importer_constants.KEY_FEED)
        self.branches = self.config.get(constants.IMPORTER_CONFIG_KEY_BRANCHES, [])
        self.remote_id = model.generate_remote_id(self.feed_url)
        self.storage_path = os.path.join(
            constants.SHARED_STORAGE, self.remote_id, constants.CONTENT_DIR)
        self.repo_id = self.get_repo().id
        self.add_child(Create())
        self.add_child(Pull())
        self.add_child(Add())
        self.add_child(Clean())


class Create(PluginStep):
    """
    Ensure the local ostree repository has been created
    and the configured.  A temporary remote is created using the repo_id as
    the remote_id.  The remote is created using the Remote which stores SSL
    certificates in the working directory.
    """

    def __init__(self):
        super(Create, self).__init__(step_type=constants.IMPORT_STEP_CREATE_REPOSITORY)
        self.description = _('Create Local Repository')

    def process_main(self):
        """
        Ensure the local ostree repository has been created
        and the configured.

        :raises PulpCodedException:
        """
        path = self.parent.storage_path
        mkdir(path)
        mkdir(os.path.join(os.path.dirname(path), constants.LINKS_DIR))
        self._init_repository(path)

    def _init_repository(self, path):
        """
        Ensure the local ostree repository has been created
        and the configured.  Also creates and configures a temporary remote
        used for the subsequent pulls.

        :param path: The absolute path to the local repository.
        :type path: str
        :raises PulpCodedException:
        """
        try:
            repository = lib.Repository(path)
            try:
                repository.open()
            except lib.LibError:
                repository.create()
            remote = Remote(self, repository)
            remote.add()
        except lib.LibError, le:
            pe = PulpCodedException(errors.OST0001, path=path, reason=str(le))
            raise pe


class Pull(PluginStep):
    """
    Pull each of the specified branches.
    """

    def __init__(self):
        super(Pull, self).__init__(step_type=constants.IMPORT_STEP_PULL)
        self.description = _('Pull Remote Branches')

    def process_main(self):
        """
        Pull each of the specified branches using the temporary remote
        configured using the repo_id as the remote_id.

        :raises PulpCodedException:
        """
        for branch_id in self.parent.branches:
            self._pull(self.parent.storage_path, self.parent.repo_id, branch_id)

    def _pull(self, path, remote_id, branch_id):
        """
        Pull the specified branch.

        :param path: The absolute path to the local repository.
        :type path: str
        :param remote_id: The remote ID.
        :type remote_id: str
        :param branch_id: The branch to pull.
        :type branch_id: str
        :raises PulpCodedException:
        """
        def report_progress(report):
            data = dict(
                b=branch_id,
                f=report.fetched,
                r=report.requested,
                p=report.percent
            )
            self.progress_details = 'branch: %(b)s fetching %(f)d/%(r)d %(p)d%%' % data
            self.report_progress(force=True)

        try:
            repository = lib.Repository(path)
            repository.pull(remote_id, [branch_id], report_progress)
        except lib.LibError, le:
            pe = PulpCodedException(errors.OST0002, branch=branch_id, reason=str(le))
            raise pe


class Add(PluginStep):
    """
    Add content units.
    """

    def __init__(self):
        super(Add, self).__init__(step_type=constants.IMPORT_STEP_ADD_UNITS)
        self.description = _('Add Content Units')

    def process_main(self):
        """
        Find all branch (heads) in the local repository and
        create content units for them.
        """
        conduit = self.get_conduit()
        repository = lib.Repository(self.parent.storage_path)
        for ref in repository.list_refs():
            if ref.path not in self.parent.branches:
                # not listed
                continue
            commit = model.Commit(ref.commit, ref.metadata)
            unit = model.Unit(self.parent.remote_id, ref.path, commit)
            self.link(unit)
            _unit = Unit(constants.OSTREE_TYPE_ID, unit.key, unit.metadata, unit.storage_path)
            conduit.save_unit(_unit)

    def link(self, unit):
        """
        Link the unit storage path to the main *content* storage path.
        The link will be verified if it already exits.

        :param unit: The unit to linked.
        :type unit: model.Unit
        """
        link = unit.storage_path
        target = self.parent.storage_path
        try:
            os.symlink(target, link)
        except OSError, e:
            if e.errno == errno.EEXIST and os.path.islink(link) and os.readlink(link) == target:
                pass  # identical
            else:
                raise


class Clean(PluginStep):
    """
    Clean up after import.
    """

    def __init__(self):
        super(Clean, self).__init__(step_type=constants.IMPORT_STEP_CLEAN)
        self.description = _('Clean')

    def process_main(self):
        """
        Clean up after import:
         - Delete the remote used for the pull.
        """
        path = self.parent.storage_path
        remote_id = self.parent.repo_id
        try:
            repository = lib.Repository(path)
            remote = lib.Remote(remote_id, repository)
            remote.delete()
        except lib.LibError, le:
            pe = PulpCodedException(errors.OST0003, id=remote_id, reason=str(le))
            raise pe


class Remote(object):
    """
    Represents an OSTree remote.
    Used to build and configure an OSTree remote.
    The complexity of configuring the remote based on the importer
    configuration is isolated here.

    :ivar step: The create step.
    :type step: Create
    :ivar repository: An OSTree repository.
    :type repository: lib.Repository
    """

    def __init__(self, step, repository):
        """
        :param step: The create step.
        :type step: Create
        :param repository: An OSTree repository.
        :type repository: lib.Repository
        """
        self.step = step
        self.repository = repository

    @property
    def url(self):
        """
        The remote URL.

        :return: The remote URL
        :rtype: str
        """
        return self.step.parent.feed_url

    @property
    def remote_id(self):
        """
        The remote ID.

        :return: The remote ID.
        :rtype: str
        """
        return self.step.parent.repo_id

    @property
    def working_dir(self):
        """
        The working directory.

        :return: The absolute path to the working directory.
        :rtype: str
        """
        return self.step.get_working_dir()

    @property
    def config(self):
        """
        The importer configuration.

        :return: The importer configuration.
        :rtype: pulp.server.plugins.config.PluginCallConfiguration
        """
        return self.step.get_config()

    @property
    def ssl_key_path(self):
        """
        The SSL private key path.

        :return: The absolute path to the private key.
        :rtype: str
        """
        path = None
        key = self.config.get(importer_constants.KEY_SSL_CLIENT_KEY)
        if key:
            path = os.path.join(self.working_dir, 'key.pem')
            with open(path, 'w+') as fp:
                fp.write(key)
            os.chmod(path, 0600)
        return path

    @property
    def ssl_cert_path(self):
        """
        The SSL client certificate key path.

        :return: The absolute path to the client certificate.
        :rtype: str
        """
        path = None
        key = self.config.get(importer_constants.KEY_SSL_CLIENT_CERT)
        if key:
            path = os.path.join(self.working_dir, 'cert.pem')
            with open(path, 'w+') as fp:
                fp.write(key)
        return path

    @property
    def ssl_ca_path(self):
        """
        The SSL CA certificate key path.

        :return: The absolute path to the CA certificate.
        :rtype: str
        """
        path = None
        key = self.config.get(importer_constants.KEY_SSL_CA_CERT)
        if key:
            path = os.path.join(self.working_dir, 'ca.pem')
            with open(path, 'w+') as fp:
                fp.write(key)
        return path

    @property
    def ssl_validation(self):
        """
        The SSL validation flag.

        :return: True if SSL validation is enabled.
        :rtype: bool
        """
        return self.config.get(importer_constants.KEY_SSL_VALIDATION, False)

    @property
    def gpg_keys(self):
        """
        The GPG keyring path and list of key IDs.

        :return: A tuple of: (path, key_ids)
            The *path* is the absolute path to a keyring.
            The *key_ids* is a list of key IDs added to the keyring.
        :rtype: tuple
        """
        home = self.working_dir
        path = os.path.join(home, 'pubring.gpg')
        key_list = self.config.get(constants.IMPORTER_CONFIG_KEY_GPG_KEYS, [])
        gpg = GPG(gnupghome=home)
        map(gpg.import_keys, key_list)
        key_ids = [key['keyid'] for key in gpg.list_keys()]
        return path, key_ids

    @property
    def proxy_url(self):
        """
        The proxy URL.

        :return: The proxy URL.
        :rtype: str
        """
        url = None
        host = self.config.get(importer_constants.KEY_PROXY_HOST)
        port = self.config.get(importer_constants.KEY_PROXY_PORT)
        user = self.config.get(importer_constants.KEY_PROXY_USER)
        password = self.config.get(importer_constants.KEY_PROXY_PASS)
        if host and port:
            url = ':'.join((host, str(port)))
            if user and password:
                auth = ':'.join((user, password))
                url = '@'.join((auth, url))
        return url

    def add(self):
        """
        Add (or replace) this remote to the repository.
        """
        path, key_ids = self.gpg_keys
        impl = lib.Remote(self.remote_id, self.repository)
        impl.url = self.url
        impl.ssl_key_path = self.ssl_key_path
        impl.ssl_cert_path = self.ssl_cert_path
        impl.ssl_ca_path = self.ssl_ca_path
        impl.ssl_validation = self.ssl_validation
        impl.gpg_validation = len(key_ids) > 0
        impl.proxy_url = self.proxy_url
        impl.update()
        if key_ids:
            impl.import_key(path, key_ids)

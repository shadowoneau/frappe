# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
import email.utils
import functools
import imaplib
import socket
import time
from datetime import datetime, timedelta
from poplib import error_proto

import frappe
from frappe import _, are_emails_muted, safe_encode
from frappe.desk.form import assign_to
from frappe.email.receive import EmailServer, InboundMail, SentEmailInInboxError
from frappe.email.smtp import SMTPServer
from frappe.email.utils import get_port
from frappe.model.document import Document
from frappe.utils import cint, comma_or, cstr, parse_addr, validate_email_address
from frappe.utils.background_jobs import enqueue, get_jobs
from frappe.utils.error import raise_error_on_no_output
from frappe.utils.jinja import render_template
from frappe.utils.user import get_system_managers

OUTGOING_EMAIL_ACCOUNT_MISSING = _("Please setup default Email Account from Setup > Email > Email Account")

class SentEmailInInbox(Exception):
	pass

def cache_email_account(cache_name):
	def decorator_cache_email_account(func):
		@functools.wraps(func)
		def wrapper_cache_email_account(*args, **kwargs):
			if not hasattr(frappe.local, cache_name):
				setattr(frappe.local, cache_name, {})

			cached_accounts = getattr(frappe.local, cache_name)
			match_by = list(kwargs.values()) + ['default']
			matched_accounts = list(filter(None, [cached_accounts.get(key) for key in match_by]))
			if matched_accounts:
				return matched_accounts[0]

			matched_accounts = func(*args, **kwargs)
			cached_accounts.update(matched_accounts or {})
			return matched_accounts and list(matched_accounts.values())[0]
		return wrapper_cache_email_account
	return decorator_cache_email_account

class EmailAccount(Document):
	DOCTYPE = 'Email Account'

	def autoname(self):
		"""Set name as `email_account_name` or make title from Email Address."""
		if not self.email_account_name:
			self.email_account_name = self.email_id.split("@", 1)[0]\
				.replace("_", " ").replace(".", " ").replace("-", " ").title()

		self.name = self.email_account_name

	def validate(self):
		"""Validate Email Address and check POP3/IMAP and SMTP connections is enabled."""
		if self.email_id:
			validate_email_address(self.email_id, True)

		if self.login_id_is_different:
			if not self.login_id:
				frappe.throw(_("Login Id is required"))
		else:
			self.login_id = None

		# validate the imap settings
		if self.enable_incoming and self.use_imap and len(self.imap_folder) <= 0:
			frappe.throw(_("You need to set one IMAP folder for {0}").format(frappe.bold(self.email_id)))

		duplicate_email_account = frappe.get_all("Email Account", filters={
			"email_id": self.email_id,
			"name": ("!=", self.name)
		})
		if duplicate_email_account:
			frappe.throw(_("Email ID must be unique, Email Account already exists for {0}") \
				.format(frappe.bold(self.email_id)))

		if frappe.local.flags.in_patch or frappe.local.flags.in_test:
			return

		#if self.enable_incoming and not self.append_to:
		#	frappe.throw(_("Append To is mandatory for incoming mails"))

		if (not self.awaiting_password and not frappe.local.flags.in_install
			and not frappe.local.flags.in_patch):
			if self.password or self.smtp_server in ('127.0.0.1', 'localhost'):
				if self.enable_incoming:
					self.get_incoming_server()
					self.no_failed = 0

				if self.enable_outgoing:
					self.validate_smtp_conn()
			else:
				if self.enable_incoming or (self.enable_outgoing and not self.no_smtp_authentication):
					frappe.throw(_("Password is required or select Awaiting Password"))

		if self.notify_if_unreplied:
			if not self.send_notification_to:
				frappe.throw(_("{0} is mandatory").format(self.meta.get_label("send_notification_to")))
			for e in self.get_unreplied_notification_emails():
				validate_email_address(e, True)

		for folder in self.imap_folder:
			if self.enable_incoming and folder.append_to:
				valid_doctypes = [d[0] for d in get_append_to()]
				if folder.append_to not in valid_doctypes:
					frappe.throw(_("Append To can be one of {0}").format(comma_or(valid_doctypes)))

	def validate_smtp_conn(self):
		if not self.smtp_server:
			frappe.throw(_("SMTP Server is required"))

		server = self.get_smtp_server()
		return server.session

	def before_save(self):
		messages = []
		as_list = 1
		if not self.enable_incoming and self.default_incoming:
			self.default_incoming = False
			messages.append(_("{} has been disabled. It can only be enabled if {} is checked.")
				.format(
					frappe.bold(_('Default Incoming')),
					frappe.bold(_('Enable Incoming'))
				)
			)
		if not self.enable_outgoing and self.default_outgoing:
			self.default_outgoing = False
			messages.append(_("{} has been disabled. It can only be enabled if {} is checked.")
				.format(
						frappe.bold(_('Default Outgoing')),
						frappe.bold(_('Enable Outgoing'))
					)
				)
		if messages:
			if len(messages) == 1: (as_list, messages) = (0, messages[0])
			frappe.msgprint(messages, as_list= as_list, indicator='orange', title=_("Defaults Updated"))

	def on_update(self):
		"""Check there is only one default of each type."""
		self.check_automatic_linking_email_account()
		self.there_must_be_only_one_default()
		setup_user_email_inbox(email_account=self.name, awaiting_password=self.awaiting_password,
			email_id=self.email_id, enable_outgoing=self.enable_outgoing)

	def there_must_be_only_one_default(self):
		"""If current Email Account is default, un-default all other accounts."""
		for field in ("default_incoming", "default_outgoing"):
			if not self.get(field):
				continue

			for email_account in frappe.get_all("Email Account", filters={ field: 1 }):
				if email_account.name==self.name:
					continue

				email_account = frappe.get_doc("Email Account", email_account.name)
				email_account.set(field, 0)
				email_account.save()

	@frappe.whitelist()
	def get_domain(self, email_id):
		"""look-up the domain and then full"""
		try:
			domain = email_id.split("@")
			fields = [
				"name as domain", "use_imap", "email_server",
				"use_ssl", "smtp_server", "use_tls",
				"smtp_port", "incoming_port", "append_emails_to_sent_folder",
				"use_ssl_for_outgoing"
			]
			return frappe.db.get_value("Email Domain", domain[1], fields, as_dict=True)
		except Exception:
			pass

	def get_incoming_server(self, in_receive=False, email_sync_rule="UNSEEN"):
		"""Returns logged in POP3/IMAP connection object."""
		if frappe.cache().get_value("workers:no-internet") == True:
			return None

		args = frappe._dict({
			"email_account_name": self.email_account_name,
			"email_account": self.name,
			"host": self.email_server,
			"use_ssl": self.use_ssl,
			"username": getattr(self, "login_id", None) or self.email_id,
			"use_imap": self.use_imap,
			"email_sync_rule": email_sync_rule,
			"incoming_port": get_port(self),
			"initial_sync_count": self.initial_sync_count or 100
		})

		if self.password:
			args.password = self.get_password()

		if not args.get("host"):
			frappe.throw(_("{0} is required").format("Email Server"))

		email_server = EmailServer(frappe._dict(args))
		self.check_email_server_connection(email_server, in_receive)

		if not in_receive and self.use_imap:
			email_server.imap.logout()

		# reset failed attempts count
		self.set_failed_attempts_count(0)

		return email_server

	def check_email_server_connection(self, email_server, in_receive):
		# tries to connect to email server and handles failure
		try:
			email_server.connect()
		except (error_proto, imaplib.IMAP4.error) as e:
			message = cstr(e).lower().replace(" ","")
			auth_error_codes = [
				'authenticationfailed',
				'loginfailed',
			]

			other_error_codes = [
				'err[auth]',
				'errtemporaryerror',
				'loginviayourwebbrowser'
			]

			all_error_codes = auth_error_codes + other_error_codes

			if in_receive and any(map(lambda t: t in message, all_error_codes)):
				# if called via self.receive and it leads to authentication error,
				# disable incoming and send email to System Manager
				error_message = _("Authentication failed while receiving emails from Email Account: {0}.").format(self.name)
				error_message += "<br>" + _("Message from server: {0}").format(cstr(e))
				self.handle_incoming_connect_error(description=error_message)
				return None

			elif not in_receive and any(map(lambda t: t in message, auth_error_codes)):
				SMTPServer.throw_invalid_credentials_exception()
			else:
				frappe.throw(cstr(e))

		except socket.error:
			if in_receive:
				# timeout while connecting, see receive.py connect method
				description = frappe.message_log.pop() if frappe.message_log else "Socket Error"
				if test_internet():
					self.db_set("no_failed", self.no_failed + 1)
					if self.no_failed > 2:
						self.handle_incoming_connect_error(description=description)
				else:
					frappe.cache().set_value("workers:no-internet", True)
				return None
			else:
				raise

	@property
	def _password(self):
		raise_exception = not (self.no_smtp_authentication or frappe.flags.in_test)
		return self.get_password(raise_exception=raise_exception)

	@property
	def default_sender(self):
		return email.utils.formataddr((self.name, self.get("email_id")))

	def is_exists_in_db(self):
		"""Some of the Email Accounts we create from configs and those doesn't exists in DB.
		This is is to check the specific email account exists in DB or not.
		"""
		return self.find_one_by_filters(name=self.name)

	@classmethod
	def from_record(cls, record):
		email_account = frappe.new_doc(cls.DOCTYPE)
		email_account.update(record)
		return email_account

	@classmethod
	def find(cls, name):
		return frappe.get_doc(cls.DOCTYPE, name)

	@classmethod
	def find_one_by_filters(cls, **kwargs):
		name = frappe.db.get_value(cls.DOCTYPE, kwargs)
		return cls.find(name) if name else None

	@classmethod
	def find_from_config(cls):
		config = cls.get_account_details_from_site_config()
		return cls.from_record(config) if config else None

	@classmethod
	def create_dummy(cls):
		return cls.from_record({"sender": "notifications@example.com"})

	@classmethod
	@raise_error_on_no_output(
		keep_quiet = lambda: not cint(frappe.get_system_settings('setup_complete')),
		error_message = OUTGOING_EMAIL_ACCOUNT_MISSING, error_type = frappe.OutgoingEmailError) # noqa
	@cache_email_account('outgoing_email_account')
	def find_outgoing(cls, match_by_email=None, match_by_doctype=None, _raise_error=False):
		"""Find the outgoing Email account to use.

		:param match_by_email: Find account using emailID
		:param match_by_doctype: Find account by matching `Append To` doctype
		:param _raise_error: This is used by raise_error_on_no_output decorator to raise error.
		"""
		if match_by_email:
			match_by_email = parse_addr(match_by_email)[1]
			doc = cls.find_one_by_filters(enable_outgoing=1, email_id=match_by_email)
			if doc:
				return {match_by_email: doc}

		if match_by_doctype:
			doc = cls.find_one_by_filters(enable_outgoing=1, enable_incoming=1, append_to=match_by_doctype)
			if doc:
				return {match_by_doctype: doc}

		doc = cls.find_default_outgoing()
		if doc:
			return {'default': doc}

	@classmethod
	def find_default_outgoing(cls):
		""" Find default outgoing account.
		"""
		doc = cls.find_one_by_filters(enable_outgoing=1, default_outgoing=1)
		doc = doc or cls.find_from_config()
		return doc or (are_emails_muted() and cls.create_dummy())

	@classmethod
	def find_incoming(cls, match_by_email=None, match_by_doctype=None):
		"""Find the incoming Email account to use.
		:param match_by_email: Find account using emailID
		:param match_by_doctype: Find account by matching `Append To` doctype
		"""
		doc = cls.find_one_by_filters(enable_incoming=1, email_id=match_by_email)
		if doc:
			return doc

		doc = cls.find_one_by_filters(enable_incoming=1, append_to=match_by_doctype)
		if doc:
			return doc

		doc = cls.find_default_incoming()
		return doc

	@classmethod
	def find_default_incoming(cls):
		doc = cls.find_one_by_filters(enable_incoming=1, default_incoming=1)
		return doc

	@classmethod
	def get_account_details_from_site_config(cls):
		if not frappe.conf.get("mail_server"):
			return {}

		field_to_conf_name_map = {
			'smtp_server': {'conf_names': ('mail_server',)},
			'smtp_port': {'conf_names': ('mail_port',)},
			'use_tls': {'conf_names': ('use_tls', 'mail_login')},
			'login_id': {'conf_names': ('mail_login',)},
			'email_id': {'conf_names': ('auto_email_id', 'mail_login'), 'default': 'notifications@example.com'},
			'password': {'conf_names': ('mail_password',)},
			'always_use_account_email_id_as_sender':
				{'conf_names': ('always_use_account_email_id_as_sender',), 'default': 0},
			'always_use_account_name_as_sender_name':
				{'conf_names': ('always_use_account_name_as_sender_name',), 'default': 0},
			'name': {'conf_names': ('email_sender_name',), 'default': 'Frappe'},
			'from_site_config': {'default': True}
		}

		account_details = {}
		for doc_field_name, d in field_to_conf_name_map.items():
			conf_names, default = d.get('conf_names') or [], d.get('default')
			value = [frappe.conf.get(k) for k in conf_names if frappe.conf.get(k)]
			account_details[doc_field_name] = (value and value[0]) or default
		return account_details

	def sendmail_config(self):
		return {
			'server': self.smtp_server,
			'port': cint(self.smtp_port),
			'login': getattr(self, "login_id", None) or self.email_id,
			'password': self._password,
			'use_ssl': cint(self.use_ssl_for_outgoing),
			'use_tls': cint(self.use_tls)
		}

	def get_smtp_server(self):
		config = self.sendmail_config()
		return SMTPServer(**config)

	def handle_incoming_connect_error(self, description):
		if test_internet():
			if self.get_failed_attempts_count() > 2:
				self.db_set("enable_incoming", 0)

				for user in get_system_managers(only_name=True):
					try:
						assign_to.add({
							'assign_to': user,
							'doctype': self.doctype,
							'name': self.name,
							'description': description,
							'priority': 'High',
							'notify': 1
						})
					except assign_to.DuplicateToDoError:
						frappe.message_log.pop()
						pass
			else:
				self.set_failed_attempts_count(self.get_failed_attempts_count() + 1)
		else:
			frappe.cache().set_value("workers:no-internet", True)

	def set_failed_attempts_count(self, value):
		frappe.cache().set('{0}:email-account-failed-attempts'.format(self.name), value)

	def get_failed_attempts_count(self):
		return cint(frappe.cache().get('{0}:email-account-failed-attempts'.format(self.name)))

	def receive(self, test_mails=None):
		"""Called by scheduler to receive emails from this EMail account using POP3/IMAP."""
		exceptions = []
		inbound_mails = self.get_inbound_mails(test_mails=test_mails)
		for mail in inbound_mails:
			try:
				communication = mail.process()
				frappe.db.commit()
				# If email already exists in the system
				# then do not send notifications for the same email.
				if communication and mail.flags.is_new_communication:
					# notify all participants of this thread
					if self.enable_auto_reply:
						self.send_auto_reply(communication, mail)

					communication.send_email(is_inbound_mail_communcation=True)
			except SentEmailInInboxError:
				frappe.db.rollback()
			except Exception:
				frappe.db.rollback()
				frappe.log_error('email_account.receive')
				if self.use_imap:
					self.handle_bad_emails(mail.uid, mail.raw_message, frappe.get_traceback())
				exceptions.append(frappe.get_traceback())
			else:
				frappe.db.commit()

		#notify if user is linked to account
		if len(inbound_mails)>0 and not frappe.local.flags.in_test:
			frappe.publish_realtime('new_email',
				{"account":self.email_account_name, "number":len(inbound_mails)}
			)

		if exceptions:
			raise Exception(frappe.as_json(exceptions))

	def get_inbound_mails(self, test_mails=None):
		"""retrive and return inbound mails.

		"""
		mails = []

		def process_mail(messages):
			for index, message in enumerate(messages.get("latest_messages", [])):
				uid = messages['uid_list'][index] if messages.get('uid_list') else None
				seen_status = 1 if messages.get('seen_status', {}).get(uid) == 'SEEN' else 0
				mails.append(InboundMail(message, self, uid, seen_status))

		if frappe.local.flags.in_test:
			return [InboundMail(msg, self) for msg in test_mails or []]

		if not self.enable_incoming:
			return []

		email_sync_rule = self.build_email_sync_rule()
		try:
			email_server = self.get_incoming_server(in_receive=True, email_sync_rule=email_sync_rule)
			if self.use_imap:
				# process all given imap folder
				for folder in self.imap_folder:
					email_server.select_imap_folder(folder.folder_name)
					email_server.settings['uid_validity'] = folder.uidvalidity
					messages = email_server.get_messages(folder=folder.folder_name) or {}
					process_mail(messages)
			else:
				# process the pop3 account
				messages = email_server.get_messages() or {}
				process_mail(messages)
			# close connection to mailserver
			email_server.logout()
		except Exception:
			frappe.log_error(title=_("Error while connecting to email account {0}").format(self.name))
			return []

		return mails

	def handle_bad_emails(self, uid, raw, reason):
		if cint(self.use_imap):
			import email
			try:
				if isinstance(raw, bytes):
					mail = email.message_from_bytes(raw)
				else:
					mail = email.message_from_string(raw)

				message_id = mail.get('Message-ID')
			except Exception:
				message_id = "can't be parsed"

			unhandled_email = frappe.get_doc({
				"raw": raw,
				"uid": uid,
				"reason":reason,
				"message_id": message_id,
				"doctype": "Unhandled Email",
				"email_account": self.name
			})
			unhandled_email.insert(ignore_permissions=True)
			frappe.db.commit()

	def send_auto_reply(self, communication, email):
		"""Send auto reply if set."""
		from frappe.core.doctype.communication.email import set_incoming_outgoing_accounts
		if self.enable_auto_reply:
			set_incoming_outgoing_accounts(communication)

			unsubscribe_message = (self.send_unsubscribe_message and _("Leave this conversation")) or ""

			frappe.sendmail(recipients = [email.from_email],
				sender = self.email_id,
				reply_to = communication.incoming_email_account,
				subject = " ".join([_("Re:"), communication.subject]),
				content = render_template(self.auto_reply_message or "", communication.as_dict()) or \
					 frappe.get_template("templates/emails/auto_reply.html").render(communication.as_dict()),
				reference_doctype = communication.reference_doctype,
				reference_name = communication.reference_name,
				in_reply_to = email.mail.get("Message-Id"), # send back the Message-Id as In-Reply-To
				unsubscribe_message = unsubscribe_message)

	def get_unreplied_notification_emails(self):
		"""Return list of emails listed"""
		self.send_notification_to = self.send_notification_to.replace(",", "\n")
		out = [e.strip() for e in self.send_notification_to.split("\n") if e.strip()]
		return out

	def on_trash(self):
		"""Clear communications where email account is linked"""
		Communication = frappe.qb.DocType("Communication")
		frappe.qb.update(Communication) \
			.set(Communication.email_account, "") \
			.where(Communication.email_account == self.name).run()

		remove_user_email_inbox(email_account=self.name)

	def after_rename(self, old, new, merge=False):
		frappe.db.set_value("Email Account", new, "email_account_name", new)

	def build_email_sync_rule(self):
		if not self.use_imap:
			return "UNSEEN"

		if self.email_sync_option == "ALL":
			max_uid = get_max_email_uid(self.name)
			last_uid = max_uid + int(self.initial_sync_count or 100) if max_uid == 1 else "*"
			return "UID {}:{}".format(max_uid, last_uid)
		else:
			return self.email_sync_option or "UNSEEN"

	def mark_emails_as_read_unread(self, email_server=None, folder_name="INBOX"):
		""" mark Email Flag Queue of self.email_account mails as read"""
		if not self.use_imap:
			return

		EmailFlagQ = frappe.qb.DocType("Email Flag Queue")
		flags = (
			frappe.qb.from_(EmailFlagQ)
			.select(EmailFlagQ.name, EmailFlagQ.communication, EmailFlagQ.uid, EmailFlagQ.action)
			.where(EmailFlagQ.is_completed == 0)
			.where(EmailFlagQ.email_account == frappe.db.escape(self.name))
		).run(as_dict=True)

		uid_list = { flag.get("uid", None): flag.get("action", "Read") for flag in flags }
		if flags and uid_list:
			if not email_server:
				email_server = self.get_incoming_server()
			if not email_server:
				return
			email_server.update_flag(folder_name, uid_list=uid_list)

			# mark communication as read
			docnames =  ",".join("'%s'"%flag.get("communication") for flag in flags \
				if flag.get("action") == "Read")
			self.set_communication_seen_status(docnames, seen=1)

			# mark communication as unread
			docnames =  ",".join([ "'%s'"%flag.get("communication") for flag in flags \
				if flag.get("action") == "Unread" ])
			self.set_communication_seen_status(docnames, seen=0)

			docnames = ",".join([ "'%s'"%flag.get("name") for flag in flags ])

			EmailFlagQueue = frappe.qb.DocType("Email Flag Queue")
			frappe.qb.update(EmailFlagQueue) \
				.set(EmailFlagQueue.is_completed, 1) \
				.where(EmailFlagQueue.name.isin(docnames)).run()

	def set_communication_seen_status(self, docnames, seen=0):
		""" mark Email Flag Queue of self.email_account mails as read"""
		if not docnames:
			return
		Communication = frappe.qb.from_("Communication")
		frappe.qb.update(Communication) \
			.set(Communication.seen == seen) \
			.where(Communication.name.isin(docnames)).run()

	def check_automatic_linking_email_account(self):
		if self.enable_automatic_linking:
			if not self.enable_incoming:
				frappe.throw(_("Automatic Linking can be activated only if Incoming is enabled."))

			if frappe.db.exists("Email Account", {"enable_automatic_linking": 1, "name": ('!=', self.name)}):
				frappe.throw(_("Automatic Linking can be activated only for one Email Account."))


	def append_email_to_sent_folder(self, message):
		email_server = None
		try:
			email_server = self.get_incoming_server(in_receive=True)
		except Exception:
			frappe.log_error(title=_("Error while connecting to email account {0}").format(self.name))

		if not email_server:
			return

		email_server.connect()

		if email_server.imap:
			try:
				message = safe_encode(message)
				email_server.imap.append("Sent", "\\Seen", imaplib.Time2Internaldate(time.time()), message)
			except Exception:
				frappe.log_error()

@frappe.whitelist()
def get_append_to(doctype=None, txt=None, searchfield=None, start=None, page_len=None, filters=None):
	txt = txt if txt else ""
	email_append_to_list = []

	# Set Email Append To DocTypes via DocType
	filters = {"istable": 0, "issingle": 0, "email_append_to": 1}
	for dt in frappe.get_all("DocType", filters=filters, fields=["name", "email_append_to"]):
		email_append_to_list.append(dt.name)

	# Set Email Append To DocTypes set via Customize Form
	for dt in frappe.get_list("Property Setter", filters={"property": "email_append_to", "value": 1}, fields=["doc_type"]):
		email_append_to_list.append(dt.doc_type)

	email_append_to = [[d] for d in set(email_append_to_list) if txt in d]

	return email_append_to

def test_internet(host="8.8.8.8", port=53, timeout=3):
	"""Returns True if internet is connected

	Host: 8.8.8.8 (google-public-dns-a.google.com)
	OpenPort: 53/tcp
	Service: domain (DNS/TCP)
	"""
	try:
		socket.setdefaulttimeout(timeout)
		socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
		return True
	except Exception as ex:
		print(ex.message)
		return False

def notify_unreplied():
	"""Sends email notifications if there are unreplied Communications
		and `notify_if_unreplied` is set as true."""
	for email_account in frappe.get_all("Email Account", "name", filters={"enable_incoming": 1, "notify_if_unreplied": 1}):
		email_account = frappe.get_doc("Email Account", email_account.name)

		if email_account.use_imap:
			append_to = [folder.get("append_to") for folder in email_account.imap_folder]
		else:
			append_to = email_account.append_to

		if append_to:
			# get open communications younger than x mins, for given doctype
			for comm in frappe.get_all("Communication", "name", filters=[
					{"sent_or_received": "Received"},
					{"reference_doctype": ("in", append_to)},
					{"unread_notification_sent": 0},
					{"email_account":email_account.name},
					{"creation": ("<", datetime.now() - timedelta(seconds = (email_account.unreplied_for_mins or 30) * 60))},
					{"creation": (">", datetime.now() - timedelta(seconds = (email_account.unreplied_for_mins or 30) * 60 * 3))}
				]):
				comm = frappe.get_doc("Communication", comm.name)

				if frappe.db.get_value(comm.reference_doctype, comm.reference_name, "status")=="Open":
					# if status is still open
					frappe.sendmail(recipients=email_account.get_unreplied_notification_emails(),
						content=comm.content, subject=comm.subject, doctype= comm.reference_doctype,
						name=comm.reference_name)

				# update flag
				comm.db_set("unread_notification_sent", 1)

def pull(now=False):
	"""Will be called via scheduler, pull emails from all enabled Email accounts."""
	if frappe.cache().get_value("workers:no-internet") == True:
		if test_internet():
			frappe.cache().set_value("workers:no-internet", False)
		else:
			return
	queued_jobs = get_jobs(site=frappe.local.site, key='job_name')[frappe.local.site]
	for email_account in frappe.get_list("Email Account",
		filters={"enable_incoming": 1, "awaiting_password": 0}):
		if now:
			pull_from_email_account(email_account.name)

		else:
			# job_name is used to prevent duplicates in queue
			job_name = 'pull_from_email_account|{0}'.format(email_account.name)

			if job_name not in queued_jobs:
				enqueue(pull_from_email_account, 'short', event='all', job_name=job_name,
					email_account=email_account.name)

def pull_from_email_account(email_account):
	'''Runs within a worker process'''
	email_account = frappe.get_doc("Email Account", email_account)
	email_account.receive()

def get_max_email_uid(email_account):
	# get maximum uid of emails
	max_uid = 1

	result = frappe.db.get_all("Communication", filters={
		"communication_medium": "Email",
		"sent_or_received": "Received",
		"email_account": email_account
	}, fields=["max(uid) as uid"])

	if not result:
		return 1
	else:
		max_uid = cint(result[0].get("uid", 0)) + 1
		return max_uid


def setup_user_email_inbox(email_account, awaiting_password, email_id, enable_outgoing):
	""" setup email inbox for user """
	from frappe.core.doctype.user.user import ask_pass_update

	def add_user_email(user):
		user = frappe.get_doc("User", user)
		row = user.append("user_emails", {})

		row.email_id = email_id
		row.email_account = email_account
		row.awaiting_password = awaiting_password or 0
		row.enable_outgoing = enable_outgoing or 0

		user.save(ignore_permissions=True)

	update_user_email_settings = False
	if not all([email_account, email_id]):
		return

	user_names = frappe.db.get_values("User", {"email": email_id}, as_dict=True)
	if not user_names:
		return

	for user in user_names:
		user_name = user.get("name")

		# check if inbox is alreay configured
		user_inbox = frappe.db.get_value("User Email", {
			"email_account": email_account,
			"parent": user_name
		}, ["name"]) or None

		if not user_inbox:
			add_user_email(user_name)
		else:
			# update awaiting password for email account
			update_user_email_settings = True

	if update_user_email_settings:
		UserEmail = frappe.qb.from_("User Email")
		frappe.qb.update(UserEmail) \
			.set(UserEmail.awaiting_password == awaiting_password or 0) \
			.set(UserEmail.enable_outgoing == enable_outgoing) \
			.where(UserEmail.email_account == email_account).run()

	else:
		users = " and ".join([frappe.bold(user.get("name")) for user in user_names])
		frappe.msgprint(_("Enabled email inbox for user {0}").format(users))
	ask_pass_update()

def remove_user_email_inbox(email_account):
	""" remove user email inbox settings if email account is deleted """
	if not email_account:
		return

	users = frappe.get_all("User Email", filters={
		"email_account": email_account
	}, fields=["parent as name"])

	for user in users:
		doc = frappe.get_doc("User", user.get("name"))
		to_remove = [row for row in doc.user_emails if row.email_account == email_account]
		[doc.remove(row) for row in to_remove]

		doc.save(ignore_permissions=True)

@frappe.whitelist(allow_guest=False)
def set_email_password(email_account, user, password):
	account = frappe.get_doc("Email Account", email_account)
	if account.awaiting_password:
		account.awaiting_password = 0
		account.password = password
		try:
			account.save(ignore_permissions=True)
		except Exception:
			frappe.db.rollback()
			return False

	return True

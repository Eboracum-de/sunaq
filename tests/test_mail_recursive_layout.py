from email.message import EmailMessage
from pathlib import Path

from rag.mail_sync import (
    MailboxSpec,
    StateDB,
    _imap_modified_utf7_decode,
    _imap_modified_utf7_encode,
    _parse_imap_list_line,
    discover_mailboxes,
    sync_mailbox,
)


class ListClient:
    def list(self, directory='""', pattern='*'):
        assert directory == '""'
        assert pattern == '*'
        return 'OK', [
            br'(\HasChildren) "/" "INBOX"',
            br'(\HasChildren) "/" "INBOX/Projekte"',
            br'(\HasNoChildren) "/" "INBOX/Projekte/2026"',
            br'(\Noselect \HasChildren) "/" "INBOX/Container"',
            br'(\HasNoChildren) "/" "INBOX/Container/Kind"',
            br'(\HasNoChildren) "/" "Sent"',
            br'(\HasNoChildren) "/" "INBOX2"',
        ]


def test_modified_utf7_roundtrip_and_list_parser():
    name = 'INBOX/Entwürfe & Reise'
    encoded = _imap_modified_utf7_encode(name)
    assert _imap_modified_utf7_decode(encoded) == name
    parsed = _parse_imap_list_line(
        ('(\\HasNoChildren) "/" "' + encoded + '"').encode('ascii')
    )
    assert parsed is not None
    flags, delimiter, mailbox = parsed
    assert '\\hasnochildren' in flags
    assert delimiter == '/'
    assert mailbox == name


def test_configured_mailbox_is_recursive_root_and_noselect_parent_is_skipped():
    specs = discover_mailboxes(ListClient(), ['INBOX'])
    assert [spec.name for spec in specs] == [
        'INBOX',
        'INBOX/Projekte',
        'INBOX/Projekte/2026',
        'INBOX/Container/Kind',
    ]
    assert specs[-1].storage_parts == ('INBOX', 'Container', 'Kind')


class SyncClient:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.selected = None

    def select(self, mailbox, readonly=True):
        self.selected = mailbox
        return 'OK', [b'1']

    def response(self, name):
        assert name == 'UIDVALIDITY'
        return name, [b'123']

    def uid(self, command, *args):
        if command == 'search':
            return 'OK', [b'1']
        if command == 'fetch':
            return 'OK', [(b'1 (BODY[] {1})', self.raw)]
        raise AssertionError((command, args))


class RecordingDAV:
    def __init__(self):
        self.dirs = []
        self.puts = []

    def ensure_dir(self, parts):
        self.dirs.append(tuple(parts))

    def put(self, parts, data, content_type):
        self.puts.append((tuple(parts), data, content_type))


def _raw_mail() -> bytes:
    msg = EmailMessage()
    msg['From'] = 'Alice <alice@example.org>'
    msg['To'] = 'Bob <bob@example.org>'
    msg['Subject'] = 'Projektstatus September'
    msg['Message-ID'] = '<m1@example.org>'
    msg['Date'] = 'Wed, 9 Sep 2026 18:40:00 +0200'
    msg.set_content('Hallo Bob')
    msg.add_attachment(b'PDFDATA', maintype='application', subtype='pdf', filename='Bericht.pdf')
    return msg.as_bytes()


def test_mail_sync_uses_one_directory_per_message(tmp_path: Path):
    client = SyncClient(_raw_mail())
    dav = RecordingDAV()
    state = StateDB(tmp_path / 'mail_state.sqlite')
    try:
        imported, available, touched = sync_mailbox(
            {
                'name': 'main',
                '_state_key': 'user:account',
                'max_messages_per_run': 250,
                'store_eml': True,
                'store_attachments': True,
            },
            'INBOX/Projekte',
            state,
            dav,
            ['Mailarchiv'],
            ['Mailarchiv'],
            None,
            False,
            mailbox_parts=MailboxSpec('INBOX/Projekte', '/').storage_parts,
            client=client,
        )
    finally:
        state.close()

    assert imported == 1
    assert available == 1
    assert client.selected == '"INBOX/Projekte"'
    assert touched == {('Mailarchiv', 'main', 'INBOX', 'Projekte', '2026', '09')}

    paths = [parts for parts, _, _ in dav.puts]
    mail_txt = next(path for path in paths if path[-1] == 'mail.txt')
    mail_dir = mail_txt[:-1]
    assert mail_dir[:6] == ('Mailarchiv', 'main', 'INBOX', 'Projekte', '2026', '09')
    assert mail_dir[-1].startswith('20260909-184000_1_Projektstatus September')
    assert mail_dir + ('.mailmeta.json',) in paths
    assert mail_dir + ('a01_Bericht.pdf',) in paths
    assert mail_dir + ('message.eml',) in paths


class CutoffSyncClient:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.selected = None
        self.fetched_uids = []

    def select(self, mailbox, readonly=True):
        self.selected = mailbox
        return 'OK', [b'4']

    def response(self, name):
        assert name == 'UIDVALIDITY'
        return name, [b'123']

    def uid(self, command, *args):
        if command == 'search':
            if args == (None, 'ALL'):
                return 'OK', [b'1 2 3 4']
            if args == (None, 'SINCE', '01-Jan-2020'):
                return 'OK', [b'2 4']
            raise AssertionError((command, args))
        if command == 'fetch':
            uid = int(args[0])
            self.fetched_uids.append(uid)
            return 'OK', [(f'{uid} (BODY[] {{1}})'.encode(), self.raw)]
        raise AssertionError((command, args))


def test_mail_sync_reopens_backfill_when_cutoff_is_removed(tmp_path: Path):
    client = CutoffSyncClient(_raw_mail())
    dav = RecordingDAV()
    state = StateDB(tmp_path / 'mail_state.sqlite')
    account = {
        'name': 'main',
        '_state_key': 'user:account',
        'not_before': '2020-01-01',
        'max_messages_per_run': 250,
        'store_eml': False,
        'store_attachments': False,
    }
    try:
        imported, available, _ = sync_mailbox(
            account, 'INBOX', state, dav, ['Mailarchiv'], ['Mailarchiv'],
            None, False, client=client,
        )
        assert imported == 2
        assert available == 2
        assert client.fetched_uids == [4, 2]

        client.fetched_uids.clear()
        account['not_before'] = ''
        imported, available, _ = sync_mailbox(
            account, 'INBOX', state, dav, ['Mailarchiv'], ['Mailarchiv'],
            None, False, client=client,
        )
        assert imported == 4
        assert available == 4
        assert client.fetched_uids == [4, 3, 2, 1]

        client.fetched_uids.clear()
        imported, available, _ = sync_mailbox(
            account, 'INBOX', state, dav, ['Mailarchiv'], ['Mailarchiv'],
            None, False, client=client,
        )
        assert imported == 0
        assert available == 0
        assert client.fetched_uids == []

        assert state.cutoff_scope('user:account', 'INBOX') == ''
    finally:
        state.close()

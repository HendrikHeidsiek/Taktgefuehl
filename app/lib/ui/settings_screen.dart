import 'dart:async';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../api.dart';
import '../models.dart';
import '../state/data.dart';
import '../state/session.dart';
import 'ai_screen.dart';
import 'theme.dart';
import 'widgets.dart';

class SettingsScreen extends StatelessWidget {
  const SettingsScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final store = context.watch<Store>();
    final session = context.watch<Session>();
    final s = store.status;
    final c = context.c;
    return Scaffold(
      backgroundColor: c.grouped,
      body: ListView(padding: const EdgeInsets.only(bottom: 24), children: [
        SafeArea(
          bottom: false,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(32, 24, 32, 16),
            child: Text('Einstellungen', style: context.t.displaySmall),
          ),
        ),
        _YoutubeSection(status: s, server: session.serverUrl),
        _AiSection(connected: s?.ai ?? false, model: s?.aiModelName ?? s?.aiModel),
        const _TakeoutSection(),
        Section(title: 'Konto', children: [
          ListTile(
            leading: const Icon(Icons.password),
            title: const Text('Passwort ändern'),
            onTap: () => _changePassword(context),
          ),
          ListTile(
            leading: Icon(Icons.logout, color: c.danger),
            title: Text('Abmelden', style: TextStyle(color: c.danger)),
            onTap: () async {
              context.read<Store>().clear();
              await context.read<Session>().logout();
            },
          ),
        ]),
        FutureBuilder(
          future: PackageInfo.fromPlatform(),
          builder: (context, snap) => Padding(
            padding: const EdgeInsets.symmetric(horizontal: 32),
            child: Text(
              'App ${snap.data?.version ?? ''} · Server ${s?.version ?? '–'}\n${session.serverUrl}',
              style: context.t.bodySmall,
              textAlign: TextAlign.center,
            ),
          ),
        ),
      ]),
    );
  }

  Future<void> _changePassword(BuildContext context) async {
    final oldPw = TextEditingController();
    final newPw = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Passwort ändern'),
        content: Column(mainAxisSize: MainAxisSize.min, children: [
          TextField(controller: oldPw, obscureText: true, decoration: const InputDecoration(labelText: 'Bisheriges Passwort')),
          TextField(
              controller: newPw,
              obscureText: true,
              decoration: const InputDecoration(labelText: 'Neues Passwort', helperText: 'Mindestens 8 Zeichen')),
        ]),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Abbrechen')),
          TextButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Ändern')),
        ],
      ),
    );
    if (ok != true || !context.mounted) return;
    try {
      await context.read<Session>().api.changePassword(oldPw.text, newPw.text);
      if (context.mounted) showMessage(context, 'Passwort geändert, andere Geräte sind abgemeldet');
    } on ApiException catch (e) {
      if (context.mounted) showMessage(context, e.message);
    }
  }
}

class _YoutubeSection extends StatelessWidget {
  final ServerStatus? status;
  final String server;
  const _YoutubeSection({required this.status, required this.server});

  String get _command => 'cd ~/Music-Player/server && .venv/bin/python -m app.auth send $server';

  /// Device-code sign-in: first time asks for the Google OAuth client, then shows the code.
  Future<void> _connectWithGoogle(BuildContext context) async {
    final store = context.read<Store>();
    try {
      DeviceCode? code;
      if (status?.oauthClient != true) {
        final client = await showDialog<(String, String)>(context: context, builder: (_) => const _OauthClientDialog());
        if (client == null || !context.mounted) return;
        code = await store.api.setOauthClient(client.$1, client.$2);
      } else {
        code = await store.api.startDeviceLogin();
      }
      if (!context.mounted) return;
      final ok = await showDialog<bool>(
          context: context, barrierDismissible: false, builder: (_) => _DeviceCodeDialog(code!, store.api));
      await store.refresh();
      if (ok == true && context.mounted) showMessage(context, 'YouTube Music ist verbunden');
    } on ApiException catch (e) {
      if (context.mounted) showMessage(context, e.message);
    }
  }

  Future<void> _paste(BuildContext context) async {
    final ctrl = TextEditingController();
    final headers = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Anfrage-Header einfügen'),
        content: TextField(
          controller: ctrl,
          maxLines: 8,
          decoration: const InputDecoration(hintText: 'accept: */*\nauthorization: SAPISIDHASH …\ncookie: …'),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Abbrechen')),
          TextButton(onPressed: () => Navigator.pop(ctx, ctrl.text), child: const Text('Verbinden')),
        ],
      ),
    );
    if (headers == null || headers.trim().isEmpty || !context.mounted) return;
    final store = context.read<Store>();
    try {
      await store.api.setYoutube(headers);
      await store.refresh();
      if (context.mounted) showMessage(context, 'YouTube Music ist verbunden');
    } on ApiException catch (e) {
      if (context.mounted) showMessage(context, e.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final c = context.c;
    final connected = status?.youtube ?? false;
    final expired = status?.youtubeExpired ?? false;
    final viaGoogle = status?.youtubeMethod == 'oauth';
    final ok = connected && !expired;
    return Section(
      title: 'YouTube Music',
      footer: 'Mit Google verbinden ist der empfohlene Weg: Du bestätigst einen Code auf google.com/device, '
          'der Server erneuert die Anmeldung danach selbst. Beim ersten Mal braucht es Client-ID und Secret '
          'aus deinem Google-Cloud-Projekt (Anleitung in der README). Die Header-Wege sind der Rückfall.',
      children: [
        ListTile(
          leading: Icon(ok ? Icons.check_circle : Icons.link_off, color: ok ? c.success : c.danger),
          title: Text(!connected ? 'Nicht verbunden' : (expired ? 'Anmeldung abgelaufen' : 'Verbunden')),
          subtitle: !connected
              ? null
              : Text(viaGoogle ? 'Über Google-Gerätecode' : 'Über kopierte Browser-Header'),
        ),
        ListTile(
          leading: Icon(Icons.verified_user_outlined, color: c.tint),
          title: Text(viaGoogle && ok ? 'Neu mit Google verbinden' : 'Mit Google verbinden'),
          subtitle: Text(status?.oauthClient == true ? 'Code auf google.com/device bestätigen'
              : 'Einmalig Google-Zugang einrichten, dann Code bestätigen'),
          onTap: () => _connectWithGoogle(context),
        ),
        ListTile(
          leading: const Icon(Icons.terminal),
          title: const Text('Header: Befehl für den Mac kopieren'),
          subtitle: Text(_command, maxLines: 2, overflow: TextOverflow.ellipsis, style: context.t.bodySmall),
          onTap: () {
            Clipboard.setData(ClipboardData(text: _command));
            showMessage(context, 'Befehl kopiert');
          },
        ),
        ListTile(
          leading: const Icon(Icons.content_paste),
          title: const Text('Header hier einfügen'),
          subtitle: const Text('Falls du sie auf dem Handy in der Zwischenablage hast'),
          onTap: () => _paste(context),
        ),
      ],
    );
  }
}

class _OauthClientDialog extends StatefulWidget {
  const _OauthClientDialog();
  @override
  State<_OauthClientDialog> createState() => _OauthClientDialogState();
}

class _OauthClientDialogState extends State<_OauthClientDialog> {
  final _id = TextEditingController();
  final _secret = TextEditingController();
  String? _error;

  void _save() {
    final id = _id.text.trim(), secret = _secret.text.trim();
    if (!id.endsWith('.apps.googleusercontent.com')) {
      return setState(() => _error = 'Die Client-ID endet auf .apps.googleusercontent.com');
    }
    if (secret.length < 5) return setState(() => _error = 'Das Secret fehlt');
    Navigator.pop(context, (id, secret));
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: const Text('Google-Zugang einrichten'),
        content: SingleChildScrollView(
          child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text('Aus deinem Google-Cloud-Projekt: OAuth-Client vom Typ „Fernseher und Geräte mit begrenzter '
                'Eingabe“, YouTube Data API aktiviert.', style: context.t.bodySmall),
            TextButton.icon(
              onPressed: () => launchUrl(Uri.parse('https://console.cloud.google.com/apis/credentials'),
                  mode: LaunchMode.externalApplication),
              icon: const Icon(Icons.open_in_new, size: 16),
              label: const Text('Google Cloud Console öffnen'),
            ),
            TextField(controller: _id, autocorrect: false, decoration: const InputDecoration(labelText: 'Client-ID')),
            TextField(
                controller: _secret,
                autocorrect: false,
                obscureText: true,
                decoration: const InputDecoration(labelText: 'Clientschlüssel (Secret)')),
            if (_error != null) ...[
              const SizedBox(height: 8),
              Text(_error!, style: context.t.bodySmall!.copyWith(color: context.c.danger)),
            ],
          ]),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Abbrechen')),
          TextButton(onPressed: _save, child: const Text('Weiter')),
        ],
      );
}

/// Shows the code, opens google.com/device and asks the server every few seconds whether it was confirmed.
class _DeviceCodeDialog extends StatefulWidget {
  final DeviceCode code;
  final Api api;
  const _DeviceCodeDialog(this.code, this.api);
  @override
  State<_DeviceCodeDialog> createState() => _DeviceCodeDialogState();
}

class _DeviceCodeDialogState extends State<_DeviceCodeDialog> {
  Timer? _timer;
  String? _error;
  bool _checking = false;

  @override
  void initState() {
    super.initState();
    _timer = Timer.periodic(Duration(seconds: widget.code.interval.clamp(3, 15)), (_) => _poll());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _poll() async {
    if (_checking) return;
    _checking = true;
    try {
      if (await widget.api.pollDeviceLogin() == 'ok' && mounted) Navigator.pop(context, true);
    } on ApiException catch (e) {
      if (e.status == 409 || e.status == 422) {
        _timer?.cancel();
        if (mounted) setState(() => _error = e.message);
      }
    } finally {
      _checking = false;
    }
  }

  Future<void> _open() async {
    await Clipboard.setData(ClipboardData(text: widget.code.userCode));
    await launchUrl(widget.code.url, mode: LaunchMode.externalApplication);
  }

  @override
  Widget build(BuildContext context) {
    final c = context.c;
    return AlertDialog(
      title: const Text('Code bei Google bestätigen'),
      content: Column(mainAxisSize: MainAxisSize.min, children: [
        Text('Öffne google.com/device, melde dich mit deinem YouTube-Music-Konto an und gib diesen Code ein:',
            style: context.t.bodyMedium),
        const SizedBox(height: 16),
        SelectableText(widget.code.userCode,
            style: context.t.headlineMedium!.copyWith(letterSpacing: 2, fontFeatures: tabular, color: c.tintText)),
        const SizedBox(height: 16),
        if (_error == null)
          Row(mainAxisAlignment: MainAxisAlignment.center, children: [
            const SizedBox(width: 14, height: 14, child: CircularProgressIndicator(strokeWidth: 2)),
            const SizedBox(width: 10),
            Text('Warte auf Bestätigung …', style: context.t.bodySmall),
          ])
        else
          Text(_error!, style: context.t.bodySmall!.copyWith(color: c.danger), textAlign: TextAlign.center),
      ]),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Abbrechen')),
        if (_error == null) FilledButton(onPressed: _open, child: const Text('Code kopieren und öffnen')),
      ],
    );
  }
}

class _AiSection extends StatelessWidget {
  final bool connected;
  final String? model;
  const _AiSection({required this.connected, this.model});

  Future<void> _setKey(BuildContext context) async {
    final ctrl = TextEditingController();
    final key = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Anthropic-API-Key'),
        content: TextField(controller: ctrl, obscureText: true, decoration: const InputDecoration(hintText: 'sk-ant-…')),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Abbrechen')),
          TextButton(onPressed: () => Navigator.pop(ctx, ctrl.text.trim()), child: const Text('Prüfen und sichern')),
        ],
      ),
    );
    if (key == null || key.isEmpty || !context.mounted) return;
    final store = context.read<Store>();
    try {
      await store.api.setAnthropic(key);
      await store.refresh();
      if (context.mounted) showMessage(context, 'KI ist eingerichtet');
    } on ApiException catch (e) {
      if (context.mounted) showMessage(context, e.message);
    }
  }

  Future<void> _remove(BuildContext context) async {
    final store = context.read<Store>();
    try {
      await store.api.deleteAnthropic();
      await store.refresh();
    } on ApiException catch (e) {
      if (context.mounted) showMessage(context, e.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final c = context.c;
    return Section(
      title: 'KI',
      footer: 'Mit Key ordnet Claude jeden Song einzeln ein, prüft neue Vorschläge und benennt die Phasen. '
          'Ohne Key nutzt der Server MusicBrainz, das ist gröber. An Anthropic gehen nur Interpreten und Songtitel.',
      children: [
        ListTile(
          leading: Icon(connected ? Icons.auto_awesome : Icons.auto_awesome_outlined,
              color: connected ? c.tint : c.tertiary),
          title: Text(connected ? 'Eingerichtet' : 'Kein Key hinterlegt'),
        ),
        if (connected) ...[
          ListTile(
            leading: const Icon(Icons.memory),
            title: const Text('Modell'),
            subtitle: model != null ? Text(model!) : null,
            trailing: const Icon(Icons.chevron_right),
            onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const ModelScreen())),
          ),
          ListTile(
            leading: const Icon(Icons.receipt_long),
            title: const Text('Kosten'),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const CostsScreen())),
          ),
        ],
        ListTile(
          leading: const Icon(Icons.key),
          title: Text(connected ? 'Key ersetzen' : 'Key eintragen'),
          onTap: () => _setKey(context),
        ),
        if (connected)
          ListTile(
            leading: Icon(Icons.delete_outline, color: c.danger),
            title: Text('Key entfernen', style: TextStyle(color: c.danger)),
            onTap: () => _remove(context),
          ),
      ],
    );
  }
}

class _TakeoutSection extends StatefulWidget {
  const _TakeoutSection();
  @override
  State<_TakeoutSection> createState() => _TakeoutSectionState();
}

class _TakeoutSectionState extends State<_TakeoutSection> {
  bool _busy = false;

  Future<void> _upload() async {
    final file = await FilePicker.pickFile(type: FileType.custom, allowedExtensions: ['json']);
    if (file == null || !mounted) return;
    final store = context.read<Store>();
    setState(() => _busy = true);
    try {
      final n = await store.api.uploadTakeout(await file.readAsBytes());
      if (!mounted) return;
      final rebuild = await showDialog<bool>(
        context: context,
        builder: (ctx) => AlertDialog(
          title: Text('$n Einträge übernommen'),
          content: const Text('Jetzt die neuen Songs einordnen und die Phasen neu berechnen? '
              'Das dauert ein paar Minuten und läuft auf dem Server weiter, auch wenn du die App schließt.'),
          actions: [
            TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Später')),
            TextButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Jetzt')),
          ],
        ),
      );
      if (rebuild == true) await store.startJob('rebuild');
    } on ApiException catch (e) {
      if (mounted) showMessage(context, e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Section(
        title: 'Ältere Daten',
        footer: 'Aus Google Takeout: „YouTube und YouTube Music“ → Verlauf, Format JSON. '
            'Die Datei heißt watch-history.json (oder Wiedergabeverlauf.json).',
        children: [
          ListTile(
            leading: _busy
                ? const SizedBox(width: 24, height: 24, child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.upload_file),
            title: Text(_busy ? 'Wird hochgeladen …' : 'Takeout-Verlauf hochladen'),
            onTap: _busy ? null : _upload,
          ),
        ],
      );
}

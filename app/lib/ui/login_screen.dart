import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../api.dart';
import '../state/data.dart';
import '../state/session.dart';
import 'theme.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});
  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  late final TextEditingController _server;
  final _pw = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _server = TextEditingController(text: context.read<Session>().serverUrl);
  }

  Future<void> _login() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await context.read<Session>().login(_server.text, _pw.text);
      if (mounted) context.read<Store>().refresh();
    } on ApiException catch (e) {
      setState(() => _error = e.message == 'Server nicht erreichbar'
          ? 'Server nicht erreichbar. Prüfe die Adresse und deine Verbindung.'
          : e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final c = context.c;
    return Scaffold(
      backgroundColor: c.grouped,
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: AutofillGroup(
                child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                  Icon(Icons.graphic_eq_rounded, size: 56, color: c.tint),
                  const SizedBox(height: 12),
                  Text('Taktgefühl', style: context.t.headlineMedium, textAlign: TextAlign.center),
                  const SizedBox(height: 6),
                  Text('Deine Hör-Phasen, deine Mixe.',
                      style: context.t.bodyMedium!.copyWith(color: c.secondary), textAlign: TextAlign.center),
                  const SizedBox(height: 32),
                  TextField(
                    controller: _server,
                    keyboardType: TextInputType.url,
                    autocorrect: false,
                    decoration: const InputDecoration(labelText: 'Server-Adresse', hintText: 'takt.example.org'),
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: _pw,
                    obscureText: true,
                    autofillHints: const [AutofillHints.password],
                    decoration: const InputDecoration(labelText: 'Passwort'),
                    onSubmitted: (_) => _busy ? null : _login(),
                  ),
                  if (_error != null) ...[
                    const SizedBox(height: 12),
                    Text(_error!, style: context.t.bodyMedium!.copyWith(color: c.danger)),
                  ],
                  const SizedBox(height: 24),
                  FilledButton(
                    onPressed: _busy ? null : _login,
                    style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(50)),
                    child: _busy
                        ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                        : const Text('Anmelden'),
                  ),
                ]),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

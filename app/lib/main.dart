import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:provider/provider.dart';

import 'state/data.dart';
import 'state/session.dart';
import 'state/updates.dart';
import 'ui/login_screen.dart';
import 'ui/shell.dart';
import 'ui/theme.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
  final session = Session();
  await session.restore();
  final store = Store(session);
  final update = AppUpdate(session);
  update.check(force: true);
  if (session.loggedIn) store.refresh();

  runApp(MultiProvider(
    providers: [
      ChangeNotifierProvider.value(value: session),
      ChangeNotifierProvider.value(value: store),
      ChangeNotifierProvider.value(value: update),
    ],
    child: const TaktgefuehlApp(),
  ));
}

class TaktgefuehlApp extends StatefulWidget {
  const TaktgefuehlApp({super.key});
  @override
  State<TaktgefuehlApp> createState() => _TaktgefuehlAppState();
}

class _TaktgefuehlAppState extends State<TaktgefuehlApp> with WidgetsBindingObserver {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      context.read<Store>().refresh();
      context.read<AppUpdate>().check();
    }
  }

  @override
  Widget build(BuildContext context) {
    final session = context.watch<Session>();
    return MaterialApp(
      title: 'Taktgefühl',
      debugShowCheckedModeBanner: false,
      theme: buildTheme(Brightness.light),
      darkTheme: buildTheme(Brightness.dark),
      locale: const Locale('de'),
      supportedLocales: const [Locale('de')],
      localizationsDelegates: GlobalMaterialLocalizations.delegates,
      home: session.loggedIn ? const Shell() : const LoginScreen(),
    );
  }
}

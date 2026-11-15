import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../api.dart';

/// Preset server address, baked in at build time:
///   flutter build apk --dart-define=DEFAULT_SERVER=https://takt.example.org
/// Empty means the login screen asks for it.
const defaultServer = String.fromEnvironment('DEFAULT_SERVER');

/// Login state, persisted so the app opens straight into the overview.
class Session extends ChangeNotifier {
  String serverUrl = defaultServer;
  String? token;

  bool get loggedIn => token != null;
  Api get api => Api(serverUrl, token);

  Future<void> restore() async {
    final p = await SharedPreferences.getInstance();
    serverUrl = p.getString('server') ?? defaultServer;
    token = p.getString('token');
    notifyListeners();
  }

  Future<void> login(String server, String password) async {
    final url = Api.normalize(server);
    final probe = Api(url, null);
    final h = await probe.health();
    if (h['app'] != 'taktgefuehl') throw ApiException('Unter dieser Adresse läuft kein Taktgefühl-Server');
    token = await probe.login(password, 'Android');
    serverUrl = url;
    final p = await SharedPreferences.getInstance();
    await p.setString('server', url);
    await p.setString('token', token!);
    notifyListeners();
  }

  Future<void> logout({bool remote = true}) async {
    if (remote) {
      try {
        await api.logout();
      } catch (_) {}
    }
    token = null;
    final p = await SharedPreferences.getInstance();
    await p.remove('token');
    notifyListeners();
  }
}

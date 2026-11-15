import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import 'models.dart';

class ApiException implements Exception {
  final int? status;
  final String message;
  ApiException(this.message, [this.status]);
  bool get unauthorized => status == 401;
  @override
  String toString() => message;
}

class Api {
  final String baseUrl;
  final String? token;
  final http.Client _client;
  Api(String baseUrl, this.token, [http.Client? client])
      : baseUrl = normalize(baseUrl),
        _client = client ?? http.Client();

  static String normalize(String url) {
    var u = url.trim();
    if (u.isEmpty) return u;
    if (!u.startsWith('http://') && !u.startsWith('https://')) u = 'https://$u';
    while (u.endsWith('/')) {
      u = u.substring(0, u.length - 1);
    }
    return u;
  }

  Map<String, String> get authHeaders => token == null ? {} : {'Authorization': 'Bearer $token'};

  Future<dynamic> _send(String method, String path,
      {Object? body, List<int>? bytes, Duration timeout = const Duration(seconds: 20)}) async {
    final req = http.Request(method, Uri.parse('$baseUrl$path'));
    req.headers.addAll(authHeaders);
    if (bytes != null) {
      req.headers['Content-Type'] = 'application/json';
      req.bodyBytes = bytes;
    } else if (body != null) {
      req.headers['Content-Type'] = 'application/json';
      req.body = jsonEncode(body);
    }
    late http.Response res;
    try {
      res = await http.Response.fromStream(await _client.send(req).timeout(timeout));
    } on TimeoutException {
      throw ApiException('Server antwortet nicht');
    } catch (e) {
      throw ApiException('Server nicht erreichbar');
    }
    if (res.statusCode >= 400) {
      String msg = 'Fehler ${res.statusCode}';
      try {
        final d = jsonDecode(utf8.decode(res.bodyBytes));
        if (d is Map && d['detail'] is String) msg = d['detail'];
        if (d is Map && d['detail'] is List) msg = 'Eingabe ungültig';
      } catch (_) {}
      throw ApiException(msg, res.statusCode);
    }
    if (res.bodyBytes.isEmpty) return null;
    return jsonDecode(utf8.decode(res.bodyBytes));
  }

  Future<Map<String, dynamic>> health() async =>
      (await _send('GET', '/api/health', timeout: const Duration(seconds: 8))) as Map<String, dynamic>;

  Future<String> login(String password, String device) async =>
      ((await _send('POST', '/api/auth/login', body: {'password': password, 'device': device})) as Map)['token']
          as String;

  Future<void> logout() async => _send('POST', '/api/auth/logout');

  Future<void> changePassword(String oldPw, String newPw) async =>
      _send('PUT', '/api/auth/password', body: {'old_password': oldPw, 'new_password': newPw});

  Future<ServerStatus> status() async => ServerStatus.fromJson(await _send('GET', '/api/status'));

  Future<JobState> startJob(String name) async => JobState.fromJson(await _send('POST', '/api/jobs/$name'));

  /// The running or last job of a lane: 'main' (sync, rebuild ...) or 'mix' (building one mix).
  Future<JobState> job({String lane = 'main'}) async =>
      JobState.fromJson(await _send('GET', '/api/jobs?lane=$lane'));

  Future<List<Phase>> phases() async =>
      ((await _send('GET', '/api/phases')) as List).map((e) => Phase.fromJson(e)).toList();

  Future<void> renamePhase(int id, String name) async => _send('PATCH', '/api/phases/$id', body: {'name': name});

  Future<List<Mix>> mixes() async => ((await _send('GET', '/api/mixes')) as List).map((e) => Mix.fromJson(e)).toList();

  Future<Mix> saveMix(String name, Map<int, double> weights, int length, double novelty, int maxMinutes, int refreshDays) async =>
      Mix.fromJson(await _send('PUT', '/api/mixes/${Uri.encodeComponent(name)}', body: {
        'weights': weights.map((k, v) => MapEntry('$k', v)),
        'length': length,
        'novelty': novelty,
        'max_minutes': maxMinutes,
        'refresh_days': refreshDays,
      }));

  Future<void> deleteMix(String name) async => _send('DELETE', '/api/mixes/${Uri.encodeComponent(name)}');

  /// Starts building the mix as a server job; it keeps running when the app closes.
  Future<JobState> buildMix(String name, {bool push = false}) async => JobState.fromJson(
      await _send('POST', '/api/mixes/${Uri.encodeComponent(name)}/build', body: {'push': push}));

  /// The songs of the last build, or null if the mix was never built.
  Future<BuildResult?> mixResult(String name) async {
    try {
      return BuildResult.fromJson(await _send('GET', '/api/mixes/${Uri.encodeComponent(name)}/result'));
    } on ApiException catch (e) {
      if (e.status == 404) return null;
      rethrow;
    }
  }

  Future<void> setYoutube(String headers) async =>
      _send('PUT', '/api/credentials/youtube', body: {'headers': headers}, timeout: const Duration(seconds: 60));

  Future<void> deleteYoutube() async => _send('DELETE', '/api/credentials/youtube');

  /// Stores the Google OAuth client and starts the device-code sign-in.
  Future<DeviceCode> setOauthClient(String id, String secret) async => DeviceCode.fromJson(await _send(
      'PUT', '/api/youtube/oauth-client', body: {'client_id': id, 'client_secret': secret}));

  Future<DeviceCode> startDeviceLogin() async => DeviceCode.fromJson(await _send('POST', '/api/youtube/device'));

  /// 'pending' until the code was confirmed at Google, then 'ok'.
  Future<String> pollDeviceLogin() async =>
      ((await _send('POST', '/api/youtube/device/poll', timeout: const Duration(seconds: 40))) as Map)['status']
          as String;

  Future<void> setAnthropic(String key) async =>
      _send('PUT', '/api/credentials/anthropic', body: {'key': key}, timeout: const Duration(seconds: 30));

  Future<void> deleteAnthropic() async => _send('DELETE', '/api/credentials/anthropic');

  Future<ModelCatalog> aiModels() async =>
      ModelCatalog.fromJson(await _send('GET', '/api/ai/models', timeout: const Duration(seconds: 60)));

  /// Fetches the model list and prices on the server now instead of waiting for the next few days.
  Future<ModelCatalog> refreshAiModels() async =>
      ModelCatalog.fromJson(await _send('POST', '/api/ai/models/refresh', timeout: const Duration(seconds: 60)));

  Future<ModelCatalog> setAiModel(String id) async =>
      ModelCatalog.fromJson(await _send('PUT', '/api/ai/model', body: {'model': id}));

  Future<CostSummary> aiCosts() async => CostSummary.fromJson(await _send('GET', '/api/ai/costs'));

  Future<int> uploadTakeout(List<int> bytes) async =>
      (((await _send('POST', '/api/takeout', bytes: bytes, timeout: const Duration(minutes: 10))) as Map)['imported']
              as num)
          .toInt();
}

import 'dart:async';

import 'package:flutter/foundation.dart';

import '../api.dart';
import '../models.dart';
import 'session.dart';

/// Everything the screens show, loaded from the server. While a job runs on the server it is polled.
class Store extends ChangeNotifier {
  final Session session;
  Store(this.session);

  ServerStatus? status;
  List<Phase> phases = [];
  List<Mix> mixes = [];
  String? error;
  bool loading = false;
  Timer? _poll;

  Api get api => session.api;

  Future<void> refresh() async {
    if (!session.loggedIn) return;
    loading = true;
    notifyListeners();
    try {
      final results = await Future.wait([api.status(), api.phases(), api.mixes()]);
      status = results[0] as ServerStatus;
      phases = results[1] as List<Phase>;
      mixes = results[2] as List<Mix>;
      error = null;
      _watchJob();
    } on ApiException catch (e) {
      if (e.unauthorized) {
        await session.logout(remote: false);
      } else {
        error = e.message;
      }
    } finally {
      loading = false;
      notifyListeners();
    }
  }

  Future<void> startJob(String name) async {
    await api.startJob(name);
    await refresh();
  }

  bool get _jobRunning => status?.job.running == true || status?.mixJob?.running == true;

  void _watchJob() {
    if (!_jobRunning || _poll != null) return;
    _poll = Timer.periodic(const Duration(seconds: 3), (_) async {
      try {
        status = await api.status();
        if (!_jobRunning) {
          _poll?.cancel();
          _poll = null;
          await refresh(); // the job changed phases, mixes or counts
        } else {
          notifyListeners();
        }
      } catch (_) {}
    });
  }

  void clear() {
    _poll?.cancel();
    _poll = null;
    status = null;
    phases = [];
    mixes = [];
    notifyListeners();
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }
}

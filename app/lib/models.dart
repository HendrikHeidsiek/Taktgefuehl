int? _int(dynamic v) => (v as num?)?.toInt();
double _dbl(dynamic v) => (v as num?)?.toDouble() ?? 0;

/// One section of a server job (e.g. "Klanganalyse") with its progress and what it left out, by reason.
class JobStep {
  final String title;
  final int? total;
  final int done;
  final int? started;
  final int? finished;
  final Map<String, int> skipped;
  JobStep.fromJson(Map<String, dynamic> j)
    : title = j['title'] as String,
      total = _int(j['total']),
      done = _int(j['done']) ?? 0,
      started = _int(j['started']),
      finished = _int(j['finished']),
      skipped = ((j['skipped'] as Map?) ?? {}).map((k, v) => MapEntry(k as String, (v as num).toInt()));

  bool get running => finished == null;
  int get skippedCount => skipped.values.fold(0, (a, b) => a + b);

  /// 0..1 when the step knows how many items it works through.
  double? get fraction => total == null || total == 0 ? null : (done / total!).clamp(0, 1).toDouble();
}

/// A log line of a job. kind: step, info, warn, skip (an item left out) or error.
class LogEntry {
  final int t;
  final String kind;
  final String text;
  final String? detail; // for skipped items: the reason
  final int? step;
  LogEntry.fromJson(Map<String, dynamic> j)
    : t = _int(j['t']) ?? 0,
      kind = j['kind'] as String? ?? 'info',
      text = j['text'] as String? ?? '',
      detail = j['detail'] as String?,
      step = _int(j['step']);
}

class JobState {
  final String? name;
  final bool running;
  final String? error;
  final int? started;
  final int? finished;
  final Map<String, dynamic> detail; // what the job works on, e.g. {'mix': 'Abend', 'push': true}
  final List<JobStep> steps;
  final List<LogEntry> entries;
  JobState.fromJson(Map<String, dynamic> j)
    : name = j['name'] as String?,
      detail = (j['detail'] as Map?)?.cast<String, dynamic>() ?? const {},
      running = j['running'] == true,
      error = j['error'] as String?,
      started = _int(j['started']),
      finished = _int(j['finished']),
      steps = ((j['steps'] as List?) ?? []).map((e) => JobStep.fromJson(e)).toList(),
      entries = ((j['entries'] as List?) ?? []).map((e) => LogEntry.fromJson(e)).toList();

  JobStep? get current => steps.isEmpty ? null : steps.last;
  int get skippedCount => steps.fold(0, (a, s) => a + s.skippedCount);

  static const labels = {
    'sync': 'Abgleich mit YouTube Music',
    'rebuild': 'Alles neu berechnen',
    'phases': 'Phasen neu berechnen',
    'mixes': 'Mixe aktualisieren',
    'mix': 'Mix erstellen',
  };
  String get label => labels[name] ?? name ?? '';

  /// The mix this job builds, if it is a mix job.
  String? get mix => name == 'mix' ? detail['mix'] as String? : null;
}

class ServerStatus {
  final String version;
  final bool youtube;
  final bool youtubeExpired;
  final String? youtubeMethod; // 'oauth' (device code) or 'browser' (copied headers)
  final bool oauthClient;
  final bool ai;
  final String aiModel;
  final String? aiModelName; // display name from the server's model list
  final bool sound;
  final Map<String, int> counts;
  final int? firstPlay;
  final int? lastSync;
  final int? lastRebuild;
  final String? lastError;
  final int? lastErrorAt;
  final int syncMinutes;
  final int rebuildHour;
  final JobState job; // sync, rebuild ...
  final JobState? mixJob; // building a single mix, runs beside the other

  ServerStatus.fromJson(Map<String, dynamic> j)
    : version = j['version'] as String,
      youtube = j['youtube'] == true,
      youtubeExpired = j['youtube_expired'] == true,
      youtubeMethod = j['youtube_method'] as String?,
      oauthClient = j['oauth_client'] == true,
      ai = j['ai']['connected'] == true,
      aiModel = j['ai']['model'] as String,
      aiModelName = j['ai']['model_name'] as String?,
      sound = j['sound'] == true,
      counts = (j['counts'] as Map).map((k, v) => MapEntry(k as String, (v as num).toInt())),
      firstPlay = _int(j['first_play']),
      lastSync = _int(j['last_sync']),
      lastRebuild = _int(j['last_rebuild']),
      lastError = (j['last_error'] as Map?)?['message'] as String?,
      lastErrorAt = _int((j['last_error'] as Map?)?['at']),
      syncMinutes = _int(j['schedule']['sync_minutes'])!,
      rebuildHour = _int(j['schedule']['rebuild_hour'])!,
      job = JobState.fromJson(j['job']),
      mixJob = j['mix_job'] == null ? null : JobState.fromJson(j['mix_job']);
}

class Song {
  final String videoId;
  final String title;
  final String? artist;
  final bool isNew;
  Song.fromJson(Map<String, dynamic> j)
    : videoId = j['video_id'] as String,
      title = j['title'] as String? ?? '?',
      artist = j['artist'] as String?,
      isNew = j['new'] == true;
  Uri get url => Uri.parse('https://music.youtube.com/watch?v=$videoId');
}

class Phase {
  final int id;
  final String name;
  final String? description;
  final String status;
  final double shareTotal;
  final double? shareRecent;
  final List<double?> weekly;
  final int songs;
  final List<String> artists;
  final List<String> topTags;
  final List<Song> topSongs;

  Phase.fromJson(Map<String, dynamic> j)
    : id = _int(j['id'])!,
      name = j['name'] as String,
      description = j['description'] as String?,
      status = j['status'] as String,
      shareTotal = _dbl(j['share_total']),
      shareRecent = (j['share_recent'] as num?)?.toDouble(),
      weekly = ((j['weekly_share'] as List?) ?? []).map((e) => (e as num?)?.toDouble()).toList(),
      songs = _int(j['songs']) ?? 0,
      artists = ((j['artists'] as List?) ?? []).cast<String>(),
      topTags = ((j['top_tags'] as List?) ?? []).cast<String>().where((t) => !t.contains(':')).toList(),
      topSongs = ((j['top_songs'] as List?) ?? []).map((e) => Song.fromJson(e)).toList();

  bool get active => status == 'aktive Phase';
}

class MixPart {
  final int phaseId;
  final String? name; // null: the phase no longer exists
  final double share;
  MixPart.fromJson(Map<String, dynamic> j)
    : phaseId = _int(j['phase_id'])!,
      name = j['name'] as String?,
      share = _dbl(j['share']);
}

class Mix {
  final String name;
  final int length;
  final double novelty;
  final int maxMinutes; // longest song in the mix, 0 = no limit
  final int refreshDays; // renewal by the server's daily run, 0 = only by hand
  final int? nextRefresh; // when that is next due; 0 = at the next run, null = only by hand
  final int? builtAt;
  final String? playlistUrl;
  final List<MixPart> parts;
  Mix.fromJson(Map<String, dynamic> j)
    : name = j['name'] as String,
      length = _int(j['length'])!,
      novelty = _dbl(j['novelty']),
      maxMinutes = _int(j['max_minutes']) ?? 0,
      refreshDays = _int(j['refresh_days']) ?? 1, // servers before 0.2.7 renewed every day
      nextRefresh = _int(j['next_refresh']),
      builtAt = _int(j['built_at']),
      playlistUrl = j['playlist_url'] as String?,
      parts = ((j['parts'] as List?) ?? []).map((e) => MixPart.fromJson(e)).toList();
}

class BuildResult {
  final Mix mix;
  final List<Song> songs;
  final int missing;
  final int? at;
  final bool pushed;
  BuildResult.fromJson(Map<String, dynamic> j)
    : mix = Mix.fromJson(j['mix']),
      at = _int(j['at']),
      pushed = j['pushed'] == true,
      songs = ((j['songs'] as List?) ?? []).map((e) => Song.fromJson(e)).toList(),
      missing = ((j['short'] as Map?) ?? {}).values.fold(0, (a, b) => a + (b as num).toInt());
}

class DeviceCode {
  final String userCode;
  final String verificationUrl;
  final int interval;
  final int expiresIn;
  DeviceCode.fromJson(Map<String, dynamic> j)
    : userCode = j['user_code'] as String,
      verificationUrl = j['verification_url'] as String,
      interval = _int(j['interval']) ?? 5,
      expiresIn = _int(j['expires_in']) ?? 1800;
  Uri get url => Uri.parse('$verificationUrl?user_code=$userCode');
}

/// Dollars per million tokens.
class ModelPrice {
  final double input;
  final double output;
  ModelPrice.fromJson(Map<String, dynamic> j) : input = _dbl(j['input']), output = _dbl(j['output']);
}

class AiModel {
  final String id;
  final String name;
  final bool deprecated;
  final ModelPrice? price;
  AiModel.fromJson(Map<String, dynamic> j)
    : id = j['id'] as String,
      name = j['name'] as String,
      deprecated = j['deprecated'] == true,
      price = j['price'] == null ? null : ModelPrice.fromJson(j['price']);
}

class ModelCatalog {
  final String selected;
  final List<AiModel> models;
  final int? at;
  final int? pricesAt;
  final String? pricesError;
  ModelCatalog.fromJson(Map<String, dynamic> j)
    : selected = j['selected'] as String,
      models = ((j['models'] as List?) ?? []).map((e) => AiModel.fromJson(e)).toList(),
      at = _int(j['at']),
      pricesAt = _int(j['prices_at']),
      pricesError = j['prices_error'] as String?;
}

/// Costs of one model or purpose within a month.
class CostPart {
  final String name;
  final double cost;
  final int calls;
  CostPart.fromJson(Map<String, dynamic> j)
    : name = j['name'] as String,
      cost = _dbl(j['cost']),
      calls = _int(j['calls']) ?? 0;
}

class CostMonth {
  final String month; // "2026-11"
  final double cost;
  final int calls;
  final int unpriced;
  final int inputTokens;
  final int outputTokens;
  final List<CostPart> models;
  final List<CostPart> purposes;
  CostMonth.fromJson(Map<String, dynamic> j)
    : month = j['month'] as String,
      cost = _dbl(j['cost']),
      calls = _int(j['calls']) ?? 0,
      unpriced = _int(j['unpriced']) ?? 0,
      inputTokens = _int(j['input_tokens']) ?? 0,
      outputTokens = _int(j['output_tokens']) ?? 0,
      models = ((j['models'] as List?) ?? []).map((e) => CostPart.fromJson(e)).toList(),
      purposes = ((j['purposes'] as List?) ?? []).map((e) => CostPart.fromJson(e)).toList();
}

class CostEntry {
  final int at;
  final String model;
  final String purpose;
  final int inputTokens;
  final int outputTokens;
  final double? cost;
  CostEntry.fromJson(Map<String, dynamic> j)
    : at = _int(j['at'])!,
      model = j['name'] as String,
      purpose = j['purpose'] as String,
      inputTokens = _int(j['input_tokens']) ?? 0,
      outputTokens = _int(j['output_tokens']) ?? 0,
      cost = (j['cost'] as num?)?.toDouble();
}

class CostSummary {
  final double total;
  final int calls;
  final double last30Days;
  final List<CostMonth> months; // newest first
  final List<CostEntry> recent;
  CostSummary.fromJson(Map<String, dynamic> j)
    : total = _dbl(j['total']),
      calls = _int(j['calls']) ?? 0,
      last30Days = _dbl(j['last_30_days']),
      months = ((j['months'] as List?) ?? []).map((e) => CostMonth.fromJson(e)).toList(),
      recent = ((j['recent'] as List?) ?? []).map((e) => CostEntry.fromJson(e)).toList();
}

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../api.dart';
import '../models.dart';
import '../state/data.dart';
import 'theme.dart';
import 'widgets.dart';

class MixesScreen extends StatelessWidget {
  const MixesScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final store = context.watch<Store>();
    final c = context.c;
    final canCreate = store.phases.isNotEmpty;
    return Scaffold(
      backgroundColor: c.grouped,
      floatingActionButton: canCreate
          ? FloatingActionButton.extended(
              onPressed: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const MixEditor())),
              icon: const Icon(Icons.add),
              label: const Text('Neuer Mix'),
            )
          : null,
      body: RefreshIndicator(
        onRefresh: store.refresh,
        child: ListView(
          padding: const EdgeInsets.only(bottom: 96),
          children: [
            SafeArea(
              bottom: false,
              child: Padding(
                padding: const EdgeInsets.fromLTRB(32, 24, 32, 4),
                child: Text('Mixe', style: context.t.displaySmall),
              ),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(32, 0, 32, 16),
              child: Text(
                'Playlists aus deinen Phasen. Der Server erneuert sie in YouTube Music, so oft du es beim Mix einstellst.',
                style: context.t.bodyMedium!.copyWith(color: c.secondary),
              ),
            ),
            if (store.mixes.isEmpty && store.status != null)
              EmptyState(
                icon: Icons.queue_music,
                title: canCreate ? 'Leg deinen ersten Mix an' : 'Erst Phasen, dann Mixe',
                text: canCreate
                    ? 'Zum Beispiel 30 % Hip-Hop und 70 % Elektronik, mit ein paar neuen Songs dazwischen.'
                    : 'Mixe setzen sich aus Phasen zusammen. Sobald welche berechnet sind, geht es hier los.',
              ),
            for (final m in store.mixes)
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
                child: Material(
                  color: c.card,
                  borderRadius: BorderRadius.circular(14),
                  clipBehavior: Clip.antiAlias,
                  child: InkWell(
                    onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => MixScreen(m.name))),
                    child: Padding(
                      padding: const EdgeInsets.all(16),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(m.name, style: context.t.titleLarge),
                          const SizedBox(height: 10),
                          _PartsBar(m.parts),
                          const SizedBox(height: 8),
                          Text(_partsText(m.parts), style: context.t.bodySmall),
                          const SizedBox(height: 4),
                          Text(
                            '${m.length} Songs · ${pct(m.novelty)} neu · ${fmtRefresh(m.refreshDays)} · '
                            '${m.builtAt == null ? 'noch nicht erstellt' : 'erneuert ${fmtRelative(m.builtAt!)}'}',
                            style: context.t.labelMedium,
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

String _partsText(List<MixPart> parts) =>
    parts.map((p) => '${pct(p.share)} ${p.name ?? '(Phase entfallen)'}').join(' · ');

/// Stacked bar of the phase shares.
class _PartsBar extends StatelessWidget {
  final List<MixPart> parts;
  const _PartsBar(this.parts);
  @override
  Widget build(BuildContext context) {
    final c = context.c;
    final shades = [c.tint, c.tint.withValues(alpha: .65), c.tint.withValues(alpha: .4), c.tint.withValues(alpha: .22)];
    return ClipRRect(
      borderRadius: BorderRadius.circular(4),
      child: SizedBox(
        height: 8,
        child: Row(
          children: [
            for (var i = 0; i < parts.length; i++)
              Expanded(
                flex: (parts[i].share * 1000).round().clamp(1, 1000),
                child: Container(
                  color: parts[i].name == null ? c.danger : shades[i % shades.length],
                  margin: EdgeInsets.only(right: i < parts.length - 1 ? 2 : 0),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class MixScreen extends StatefulWidget {
  final String name;
  const MixScreen(this.name, {super.key});
  @override
  State<MixScreen> createState() => _MixScreenState();
}

class _MixScreenState extends State<MixScreen> {
  BuildResult? _result;
  bool _busy = false;
  String? _busyText;
  String? _failed; // why the last build of this mix stopped, shown until the next one starts

  Mix? _mix(Store store) => store.mixes.where((m) => m.name == widget.name).firstOrNull;

  @override
  void initState() {
    super.initState();
    _loadResult();
    _resume();
  }

  /// Picks up a job for this mix that is still running on the server (started earlier, then the
  /// screen was left), so the screen shows its progress instead of offering to start it again.
  Future<void> _resume() async {
    try {
      final job = await context.read<Store>().api.job(lane: 'mix');
      if (job.mix != widget.name || !mounted) return;
      if (job.running) {
        await _follow(push: job.detail['push'] == true);
      } else if (job.error != null && job.finished != null && _recent(job.finished!)) {
        setState(() => _failed = job.error);
      }
    } on ApiException catch (_) {}
  }

  static bool _recent(int finished) => DateTime.now().millisecondsSinceEpoch ~/ 1000 - finished < 24 * 3600;

  Future<void> _loadResult() async {
    try {
      final r = await context.read<Store>().api.mixResult(widget.name);
      if (mounted && r != null) setState(() => _result = r);
    } on ApiException catch (_) {}
  }

  /// Starts the server job and follows it while this screen is open. Leaving the screen or closing
  /// the app does not stop it; the overview shows its progress and the result loads on the next visit.
  Future<void> _build({required bool push}) async {
    final store = context.read<Store>();
    setState(() => _busy = true); // no second tap while the job is being started
    try {
      await store.api.buildMix(widget.name, push: push);
      store.refresh(); // the overview follows the job too
    } on ApiException catch (e) {
      if (mounted) {
        showMessage(context, e.message);
        setState(() => _busy = false);
      }
      return;
    }
    if (!mounted) return;
    await _follow(push: push);
  }

  /// Shows the running build job of this mix until it ends, then loads the result.
  Future<void> _follow({required bool push}) async {
    final store = context.read<Store>();
    setState(() {
      _busy = true;
      _failed = null;
      _busyText = push ? 'Wird erstellt und zu YouTube Music geschickt …' : 'Mix wird zusammengestellt …';
    });
    try {
      var job = await store.api.job(lane: 'mix');
      while (job.running) {
        if (job.current != null) setState(() => _busyText = '${job.current!.title} …');
        await Future.delayed(const Duration(seconds: 2));
        if (!mounted) return;
        job = await store.api.job(lane: 'mix');
      }
      await _loadResult(); // also after a refused upload: the selection is kept
      await store.refresh();
      if (job.error != null) {
        if (mounted) setState(() => _failed = job.error);
        return;
      }
      if (push && mounted) showMessage(context, 'In YouTube Music aktualisiert');
    } on ApiException catch (e) {
      if (mounted) showMessage(context, e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _delete(Mix m) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('„${m.name}“ löschen?'),
        content: const Text(
          'Taktgefühl erneuert diesen Mix dann nicht mehr. '
          'Die Playlist in YouTube Music bleibt erhalten, du kannst sie dort selbst löschen.',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Abbrechen')),
          TextButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Löschen')),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    final store = context.read<Store>();
    try {
      await store.api.deleteMix(m.name);
      await store.refresh();
      if (mounted) Navigator.pop(context);
    } on ApiException catch (e) {
      if (mounted) showMessage(context, e.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final store = context.watch<Store>();
    final m = _mix(store);
    final c = context.c;
    if (m == null) return const Scaffold(body: SizedBox.shrink());
    return Scaffold(
      backgroundColor: c.grouped,
      appBar: AppBar(
        backgroundColor: c.grouped,
        title: Text(m.name),
        actions: [
          IconButton(
            tooltip: 'Bearbeiten',
            icon: const Icon(Icons.tune),
            onPressed: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => MixEditor(mix: m))),
          ),
          IconButton(tooltip: 'Löschen', icon: const Icon(Icons.delete_outline), onPressed: () => _delete(m)),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.only(top: 8, bottom: 24),
        children: [
          Section(
            title: 'Zusammensetzung',
            children: [
              Padding(padding: const EdgeInsets.all(16), child: _PartsBar(m.parts)),
              for (final p in m.parts)
                Row2(p.name ?? 'Phase entfallen', pct(p.share), valueColor: p.name == null ? c.danger : null),
              Row2('Länge', '${m.length} Songs'),
              Row2('Neue Songs', pct(m.novelty)),
              Row2('Längster Song', fmtMaxMinutes(m.maxMinutes)),
              Row2('Erneuern', fmtRefresh(m.refreshDays)),
              if (m.nextRefresh != null) Row2('Nächstes Mal', fmtNextRefresh(m.nextRefresh!)),
            ],
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                if (_failed != null && !_busy)
                  Container(
                    margin: const EdgeInsets.only(bottom: 12),
                    padding: const EdgeInsets.all(14),
                    decoration: BoxDecoration(color: context.c.card, borderRadius: BorderRadius.circular(14)),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Icon(Icons.error_outline, color: context.c.danger),
                        const SizedBox(width: 12),
                        Expanded(
                          child: Text(
                            'Letzter Versuch abgebrochen: $_failed',
                            style: context.t.bodyMedium!.copyWith(color: context.c.danger),
                          ),
                        ),
                      ],
                    ),
                  ),
                if (_busy)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 8),
                    child: Column(
                      children: [
                        const LinearProgressIndicator(),
                        const SizedBox(height: 8),
                        Text(
                          '${_busyText ?? ''}\nDu kannst die App dabei schließen, der Server macht weiter.',
                          style: context.t.bodySmall,
                          textAlign: TextAlign.center,
                        ),
                      ],
                    ),
                  ),
                FilledButton.icon(
                  onPressed: _busy ? null : () => _build(push: true),
                  icon: const Icon(Icons.cloud_upload_outlined),
                  label: const Text('Neu erstellen und zu YouTube Music'),
                  style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
                ),
                const SizedBox(height: 8),
                Row(
                  children: [
                    Expanded(
                      child: OutlinedButton(
                        onPressed: _busy ? null : () => _build(push: false),
                        child: const Text('Nur Vorschau'),
                      ),
                    ),
                    if (m.playlistUrl != null) ...[
                      const SizedBox(width: 8),
                      Expanded(
                        child: OutlinedButton.icon(
                          onPressed: () => launchUrl(Uri.parse(m.playlistUrl!), mode: LaunchMode.externalApplication),
                          icon: const Icon(Icons.open_in_new, size: 18),
                          label: const Text('Öffnen'),
                        ),
                      ),
                    ],
                  ],
                ),
              ],
            ),
          ),
          if (_result != null)
            Section(
              title:
                  '${_result!.songs.length} Songs${_result!.at != null ? ' · ${_result!.pushed ? 'erstellt' : 'Vorschau'} ${fmtRelative(_result!.at!)}' : ''}',
              footer: _result!.missing > 0
                  ? '${_result!.missing} Songs fehlen: In manchen Phasen gibt es noch nicht genug passende Songs.'
                  : null,
              children: [
                for (final (i, s) in _result!.songs.indexed)
                  ListTile(
                    dense: true,
                    leading: SizedBox(
                      width: 24,
                      child: Text('${i + 1}', style: context.t.bodySmall!.copyWith(fontFeatures: tabular)),
                    ),
                    title: Text(s.title, maxLines: 1, overflow: TextOverflow.ellipsis),
                    subtitle: Text(s.artist ?? ''),
                    trailing: s.isNew ? const Pill('neu', strong: true) : null,
                    onTap: () => launchUrl(s.url, mode: LaunchMode.externalApplication),
                  ),
              ],
            ),
        ],
      ),
    );
  }
}

/// Create or edit a mix: one slider per phase, the shares are normalised to 100 %.
/// Choices for the longest song of a mix; 0 = no limit, kept last.
const _maxSteps = [6, 8, 10, 12, 15, 20, 30, 45, 60, 0];

String fmtMaxMinutes(int minutes) => minutes == 0 ? 'keine Grenze' : 'bis $minutes min';

/// Choices for how often the server renews a mix; 0 = only by hand, kept last.
const _refreshSteps = [1, 2, 3, 7, 14, 30, 0];

String fmtRefresh(int days) => switch (days) {
  0 => 'nur von Hand',
  1 => 'täglich',
  7 => 'wöchentlich',
  14 => 'alle 2 Wochen',
  30 => 'monatlich',
  _ => 'alle $days Tage',
};

/// The date of the next renewal; the server runs it once a day in the early morning.
String fmtNextRefresh(int at) {
  final now = DateTime.now();
  final day = DateTime.fromMillisecondsSinceEpoch(at * 1000);
  if (at == 0 || !day.isAfter(now)) return 'beim nächsten Lauf';
  final days = DateTime(day.year, day.month, day.day).difference(DateTime(now.year, now.month, now.day)).inDays;
  if (days <= 1) return 'morgen';
  return 'am ${day.day}.${day.month}.';
}

class MixEditor extends StatefulWidget {
  final Mix? mix;
  const MixEditor({super.key, this.mix});
  @override
  State<MixEditor> createState() => _MixEditorState();
}

class _MixEditorState extends State<MixEditor> {
  late final TextEditingController _name;
  final Map<int, double> _weights = {};
  late double _length;
  late double _novelty;
  late int _maxIndex;
  late int _refreshIndex;
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    final m = widget.mix;
    _name = TextEditingController(text: m?.name ?? '');
    _length = (m?.length ?? 50).toDouble();
    _novelty = m?.novelty ?? .2;
    final max = m?.maxMinutes ?? 15;
    _maxIndex = _maxSteps.indexOf(max);
    if (_maxIndex < 0) _maxIndex = _maxSteps.indexWhere((s) => s == 0 || s >= max);
    final refresh = m?.refreshDays ?? 7;
    _refreshIndex = _refreshSteps.indexWhere((s) => s == 0 ? refresh == 0 : s >= refresh && refresh > 0);
    if (_refreshIndex < 0) _refreshIndex = _refreshSteps.indexOf(30);
    for (final p in m?.parts ?? <MixPart>[]) {
      if (p.name != null) _weights[p.phaseId] = (p.share * 100).roundToDouble();
    }
  }

  double get _total => _weights.values.fold(0, (a, b) => a + b);

  Future<void> _save() async {
    final name = _name.text.trim();
    final weights = {
      for (final e in _weights.entries)
        if (e.value > 0) e.key: e.value,
    };
    if (name.isEmpty) return setState(() => _error = 'Gib dem Mix einen Namen.');
    if (weights.isEmpty) return setState(() => _error = 'Wähle mindestens eine Phase.');
    setState(() {
      _busy = true;
      _error = null;
    });
    final store = context.read<Store>();
    try {
      await store.api.saveMix(name, weights, _length.round(), _novelty, _maxSteps[_maxIndex], _refreshSteps[_refreshIndex]);
      await store.refresh();
      if (!mounted) return;
      Navigator.pop(context);
      if (widget.mix == null) {
        Navigator.of(context).push(MaterialPageRoute(builder: (_) => MixScreen(name)));
      }
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final store = context.watch<Store>();
    final c = context.c;
    final editing = widget.mix != null;
    return Scaffold(
      backgroundColor: c.grouped,
      appBar: AppBar(
        backgroundColor: c.grouped,
        title: Text(editing ? 'Mix bearbeiten' : 'Neuer Mix'),
        actions: [TextButton(onPressed: _busy ? null : _save, child: const Text('Sichern'))],
      ),
      body: ListView(
        padding: const EdgeInsets.only(top: 8, bottom: 24),
        children: [
          Section(
            children: [
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
                child: TextField(
                  controller: _name,
                  enabled: !editing,
                  maxLength: 60,
                  decoration: const InputDecoration(
                    labelText: 'Name',
                    hintText: 'Feierabend',
                    border: InputBorder.none,
                  ),
                ),
              ),
            ],
          ),
          Section(
            title: 'Phasen',
            footer: 'Die Regler werden zusammen auf 100 % gerechnet.',
            children: [
              for (final p in store.phases)
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Expanded(child: Text(p.name, style: context.t.bodyLarge)),
                          Text(
                            _total == 0 || (_weights[p.id] ?? 0) == 0 ? '–' : pct((_weights[p.id] ?? 0) / _total),
                            style: context.t.bodyLarge!.copyWith(
                              color: (_weights[p.id] ?? 0) > 0 ? c.tintText : c.tertiary,
                              fontFeatures: tabular,
                            ),
                          ),
                        ],
                      ),
                      Slider(
                        value: _weights[p.id] ?? 0,
                        max: 100,
                        divisions: 20,
                        onChanged: (v) => setState(() => _weights[p.id] = v),
                      ),
                    ],
                  ),
                ),
            ],
          ),
          Section(
            title: 'Länge und Neues',
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Expanded(child: Text('Songs', style: context.t.bodyLarge)),
                        Text('${_length.round()}', style: context.t.bodyLarge!.copyWith(fontFeatures: tabular)),
                      ],
                    ),
                    Slider(
                      value: _length,
                      min: 20,
                      max: 150,
                      divisions: 26,
                      onChanged: (v) => setState(() => _length = v),
                    ),
                  ],
                ),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Expanded(child: Text('Neue Songs', style: context.t.bodyLarge)),
                        Text(pct(_novelty), style: context.t.bodyLarge!.copyWith(fontFeatures: tabular)),
                      ],
                    ),
                    Slider(value: _novelty, max: .6, divisions: 12, onChanged: (v) => setState(() => _novelty = v)),
                    Text('Unbekannte Songs, die klanglich zur jeweiligen Phase passen.', style: context.t.bodySmall),
                    const SizedBox(height: 8),
                  ],
                ),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Expanded(child: Text('Längster Song', style: context.t.bodyLarge)),
                        Text(
                          fmtMaxMinutes(_maxSteps[_maxIndex]),
                          style: context.t.bodyLarge!.copyWith(fontFeatures: tabular),
                        ),
                      ],
                    ),
                    Slider(
                      value: _maxIndex.toDouble(),
                      max: _maxSteps.length - 1.0,
                      divisions: _maxSteps.length - 1,
                      onChanged: (v) => setState(() => _maxIndex = v.round()),
                    ),
                    Text(
                      'Längere Tracks wie stundenlange DJ-Sets oder Lernmusik bleiben draußen.',
                      style: context.t.bodySmall,
                    ),
                    const SizedBox(height: 8),
                  ],
                ),
              ),
            ],
          ),
          Section(
            title: 'Erneuern',
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Expanded(child: Text('Neu zusammenstellen', style: context.t.bodyLarge)),
                        Text(fmtRefresh(_refreshSteps[_refreshIndex]), style: context.t.bodyLarge),
                      ],
                    ),
                    Slider(
                      value: _refreshIndex.toDouble(),
                      max: _refreshSteps.length - 1.0,
                      divisions: _refreshSteps.length - 1,
                      onChanged: (v) => setState(() => _refreshIndex = v.round()),
                    ),
                    Text(
                      'Jedes Erneuern sucht neue Songs und hört sie an, das kostet KI-Aufrufe und Zeit. '
                      '„Neu erstellen“ beim Mix geht immer.',
                      style: context.t.bodySmall,
                    ),
                    const SizedBox(height: 8),
                  ],
                ),
              ),
            ],
          ),
          if (_error != null)
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 32),
              child: Text(_error!, style: context.t.bodyMedium!.copyWith(color: c.danger)),
            ),
        ],
      ),
    );
  }
}

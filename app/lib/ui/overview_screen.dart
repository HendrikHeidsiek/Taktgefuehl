import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../api.dart';
import '../models.dart';
import '../state/data.dart';
import '../state/updates.dart';
import 'job_screen.dart';
import 'theme.dart';
import 'widgets.dart';

class OverviewScreen extends StatelessWidget {
  const OverviewScreen({super.key});

  Future<void> _start(BuildContext context, String job) async {
    try {
      await context.read<Store>().startJob(job);
    } on ApiException catch (e) {
      if (context.mounted) showMessage(context, e.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final store = context.watch<Store>();
    final update = context.watch<AppUpdate>();
    final s = store.status;
    final c = context.c;
    return Scaffold(
      backgroundColor: c.grouped,
      body: RefreshIndicator(
        onRefresh: store.refresh,
        child: ListView(
          padding: const EdgeInsets.only(bottom: 24),
          children: [
            SafeArea(
              bottom: false,
              child: Padding(
                padding: const EdgeInsets.fromLTRB(32, 24, 32, 16),
                child: Text('Übersicht', style: context.t.displaySmall),
              ),
            ),
            if (update.showNote)
              Section(
                children: [
                  ListTile(
                    leading: Icon(Icons.system_update, color: c.tint),
                    title: Text('Neue App-Version ${update.latestVersion ?? ''}'),
                    subtitle: const Text('Tippen zum Herunterladen'),
                    trailing: TextButton(onPressed: update.dismiss, child: const Text('Später')),
                    onTap: update.download,
                  ),
                ],
              ),
            if (store.error != null && s == null)
              EmptyState(
                icon: Icons.cloud_off,
                title: 'Server nicht erreichbar',
                text: store.error!,
                action: OutlinedButton(onPressed: store.refresh, child: const Text('Erneut versuchen')),
              ),
            if (s == null && store.error == null)
              const Padding(
                padding: EdgeInsets.all(48),
                child: Center(child: CircularProgressIndicator()),
              ),
            if (s != null) ...[
              if (s.youtube && s.youtubeExpired)
                Section(
                  children: [
                    ListTile(
                      leading: Icon(Icons.link_off, color: c.danger),
                      title: const Text('YouTube-Anmeldung abgelaufen'),
                      subtitle: const Text(
                        'Unter Einstellungen → YouTube Music neu verbinden. '
                        'Bis dahin gleicht der Server nicht ab.',
                      ),
                    ),
                  ],
                ),
              if (!s.youtube)
                Section(
                  children: [
                    ListTile(
                      leading: Icon(Icons.link_off, color: c.danger),
                      title: const Text('YouTube Music ist nicht verbunden'),
                      subtitle: const Text('Unter Einstellungen → YouTube Music verbinden.'),
                    ),
                  ],
                ),
              _ActivePhases(store.phases),
              _JobSection(s, onStart: (job) => _start(context, job)),
              Section(
                title: 'Deine Daten',
                children: [
                  Row2('Songs', '${s.counts['tracks']}'),
                  Row2('Likes', '${s.counts['liked']}'),
                  Row2('Wiedergaben mit Uhrzeit', '${s.counts['plays_timed']}'),
                  if ((s.counts['plays_mix'] ?? 0) > 0)
                    Row2('davon aus Taktgefühl-Mixen', '${s.counts['plays_mix']}'),
                  if (s.sound) Row2('Songs angehört (Klang)', '${s.counts['sound'] ?? 0}'),
                  if (s.firstPlay != null) Row2('Verlauf seit', fmtDate(s.firstPlay!)),
                  Row2('Phasen', '${s.counts['phases']}'),
                  Row2('Mixe', '${s.counts['mixes']}'),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _ActivePhases extends StatelessWidget {
  final List<Phase> phases;
  const _ActivePhases(this.phases);

  @override
  Widget build(BuildContext context) {
    if (phases.isEmpty) return const SizedBox.shrink();
    final recent = [...phases.where((p) => p.shareRecent != null)]
      ..sort((a, b) => (b.shareRecent ?? 0).compareTo(a.shareRecent ?? 0));
    final top = recent.take(3).toList();
    if (top.isEmpty) return const SizedBox.shrink();
    return Section(
      title: 'Gerade hörst du vor allem',
      footer: 'Anteil an deinen Wiedergaben der letzten zwei Wochen.',
      children: [
        for (final p in top)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Expanded(child: Text(p.name, style: context.t.titleMedium)),
                    if (p.active) const Pill('aktive Phase', strong: true),
                    const SizedBox(width: 8),
                    Text(pct(p.shareRecent!), style: context.t.bodyMedium!.copyWith(fontFeatures: tabular)),
                  ],
                ),
                const SizedBox(height: 8),
                ShareBar(p.shareRecent!),
              ],
            ),
          ),
      ],
    );
  }
}

class _JobSection extends StatelessWidget {
  final ServerStatus s;
  final void Function(String job) onStart;
  const _JobSection(this.s, {required this.onStart});

  @override
  Widget build(BuildContext context) {
    final c = context.c;
    final job = s.job;
    return Section(
      title: 'Abgleich',
      footer:
          'Der Server gleicht alle ${s.syncMinutes} Minuten mit YouTube Music ab und berechnet ab '
          '${s.rebuildHour} Uhr einmal täglich Phasen und Mixe neu.',
      children: [
        Row2('Letzter Abgleich', s.lastSync == null ? 'noch nie' : fmtRelative(s.lastSync!)),
        Row2('Zuletzt neu berechnet', s.lastRebuild == null ? 'noch nie' : fmtRelative(s.lastRebuild!)),
        if (job.name != null) _JobRow(job, lane: 'main'),
        // A mix job shows while it runs, and for a few hours after it stopped with an error.
        if (s.mixJob != null &&
            (s.mixJob!.running ||
                (s.mixJob!.error != null &&
                    s.mixJob!.finished != null &&
                    DateTime.now().millisecondsSinceEpoch ~/ 1000 - s.mixJob!.finished! < 6 * 3600)))
          _JobRow(s.mixJob!, lane: 'mix'),
        if (!job.running &&
            s.lastError != null &&
            job.error == null &&
            s.lastErrorAt != null &&
            (s.lastSync ?? 0) < s.lastErrorAt!)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
            child: Text(
              'Letzter Fehler (${fmtRelative(s.lastErrorAt!)}): ${s.lastError}',
              style: context.t.bodySmall!.copyWith(color: c.danger),
            ),
          ),
        Padding(
          padding: const EdgeInsets.all(12),
          child: Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              FilledButton.tonal(
                onPressed: job.running || !s.youtube ? null : () => onStart('sync'),
                child: const Text('Jetzt abgleichen'),
              ),
              OutlinedButton(
                onPressed: job.running || !s.youtube ? null : () => onStart('rebuild'),
                child: const Text('Alles neu berechnen'),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

/// A job in short: what runs, the current step with its progress, what was skipped. Tap for the log.
class _JobRow extends StatelessWidget {
  final JobState job;
  final String lane;
  const _JobRow(this.job, {required this.lane});

  @override
  Widget build(BuildContext context) {
    final c = context.c;
    final title = job.mix != null ? 'Mix „${job.mix}“' : job.label;
    final step = job.current;
    final String subtitle;
    if (job.running) {
      final skipped = job.skippedCount;
      subtitle = step == null ? 'startet …' : '${step.title}${skipped > 0 ? ' · $skipped übersprungen' : ''}';
    } else if (job.error != null) {
      subtitle = 'Abgebrochen ${job.finished == null ? '' : fmtRelative(job.finished!)}: ${job.error}';
    } else {
      final skipped = job.skippedCount;
      subtitle =
          'Fertig ${job.finished == null ? '' : fmtRelative(job.finished!)}'
          '${skipped > 0 ? ' · $skipped übersprungen' : ''}';
    }
    return InkWell(
      onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => JobScreen(lane: lane))),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 8, 12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                SizedBox(
                  width: 20,
                  child: job.running
                      ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                      : Icon(
                          job.error == null ? Icons.check_circle : Icons.error,
                          size: 18,
                          color: job.error == null ? c.success : c.danger,
                        ),
                ),
                const SizedBox(width: 10),
                Expanded(child: Text(job.running ? '$title läuft' : title, style: context.t.titleMedium)),
                if (job.running && step != null)
                  Text(
                    stepValue(step),
                    style: context.t.bodyMedium!.copyWith(color: c.secondary, fontFeatures: tabular),
                  ),
                Icon(Icons.chevron_right, color: c.tertiary),
              ],
            ),
            Padding(
              padding: const EdgeInsets.only(left: 30, top: 4, right: 8),
              child: Text(
                subtitle,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: context.t.bodySmall!.copyWith(color: job.error != null && !job.running ? c.danger : null),
              ),
            ),
            if (job.running && step?.fraction != null)
              Padding(
                padding: const EdgeInsets.only(left: 30, top: 8, right: 8),
                child: ClipRRect(
                  borderRadius: BorderRadius.circular(3),
                  child: LinearProgressIndicator(value: step!.fraction, minHeight: 6),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

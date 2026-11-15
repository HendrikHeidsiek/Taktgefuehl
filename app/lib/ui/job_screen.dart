import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../api.dart';
import '../models.dart';
import '../state/data.dart';
import 'theme.dart';
import 'widgets.dart';

const _warn = Color(0xFFFF9F0A); // systemOrange

String fmtClock(int seconds) {
  final t = DateTime.fromMillisecondsSinceEpoch(seconds * 1000);
  String two(int v) => v.toString().padLeft(2, '0');
  return '${two(t.hour)}:${two(t.minute)}:${two(t.second)}';
}

String fmtDuration(int seconds) {
  if (seconds < 60) return '$seconds s';
  if (seconds < 3600) return '${seconds ~/ 60} min';
  return '${seconds ~/ 3600} h ${(seconds % 3600) ~/ 60} min';
}

/// "20/601" or the duration of a finished step.
String stepValue(JobStep s) {
  if (s.running && s.total != null) return '${s.done}/${s.total}';
  if (s.running || s.started == null) return '';
  return fmtDuration(s.finished! - s.started!);
}

/// One step with its progress bar and what it skipped; used in the overview and in the full log.
class StepTile extends StatelessWidget {
  final JobStep step;
  final bool failed;
  const StepTile(this.step, {super.key, this.failed = false});

  @override
  Widget build(BuildContext context) {
    final c = context.c;
    final s = step;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              SizedBox(
                width: 20,
                child: s.running && !failed
                    ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                    : Icon(failed ? Icons.error : Icons.check_circle, size: 18, color: failed ? c.danger : c.success),
              ),
              const SizedBox(width: 10),
              Expanded(child: Text(s.title, style: context.t.bodyLarge)),
              Text(
                stepValue(s),
                style: context.t.bodyMedium!.copyWith(color: c.secondary, fontFeatures: tabular),
              ),
            ],
          ),
          if (s.running && !failed && s.fraction != null) ...[
            const SizedBox(height: 8),
            Padding(
              padding: const EdgeInsets.only(left: 30),
              child: ClipRRect(
                borderRadius: BorderRadius.circular(3),
                child: LinearProgressIndicator(value: s.fraction, minHeight: 6),
              ),
            ),
          ],
          if (s.skippedCount > 0) ...[
            const SizedBox(height: 6),
            Padding(
              padding: const EdgeInsets.only(left: 30),
              child: Text(
                [
                  '${s.skippedCount} übersprungen',
                  for (final e in s.skipped.entries.toList()..sort((a, b) => b.value - a.value))
                    '${e.value} × ${e.key}',
                ].join('\n'),
                style: context.t.bodySmall,
              ),
            ),
          ],
        ],
      ),
    );
  }
}

/// The whole log of a job lane: steps with progress, then the entries, refreshed while it runs.
class JobScreen extends StatefulWidget {
  final String lane;
  const JobScreen({super.key, this.lane = 'main'});
  @override
  State<JobScreen> createState() => _JobScreenState();
}

class _JobScreenState extends State<JobScreen> {
  JobState? _job;
  String? _error;
  bool _all = false;
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _load();
    _timer = Timer.periodic(const Duration(seconds: 2), (_) {
      if (_job?.running ?? true) _load();
    });
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final j = await context.read<Store>().api.job(lane: widget.lane);
      if (mounted) setState(() => _job = j);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final c = context.c;
    final job = _job;
    return Scaffold(
      backgroundColor: c.grouped,
      appBar: AppBar(backgroundColor: c.grouped, title: Text(job?.name == null ? 'Protokoll' : _title(job!))),
      body: job == null
          ? Center(child: _error == null ? const CircularProgressIndicator() : Text(_error!))
          : job.name == null
          ? const EmptyState(icon: Icons.receipt_long, title: 'Noch nichts gelaufen', text: '')
          : _body(context, job),
    );
  }

  String _title(JobState job) => job.mix != null ? 'Mix „${job.mix}“' : job.label;

  Widget _body(BuildContext context, JobState job) {
    final c = context.c;
    final entries = (_all ? job.entries : job.entries.where((e) => e.kind != 'skip')).toList().reversed.toList();
    final skipped = job.entries.where((e) => e.kind == 'skip').length;
    return ListView(
      padding: const EdgeInsets.only(top: 8, bottom: 24),
      children: [
        Section(
          children: [
            Row2(
              job.running ? 'Läuft seit' : (job.error != null ? 'Abgebrochen' : 'Fertig'),
              job.running
                  ? fmtDuration(DateTime.now().millisecondsSinceEpoch ~/ 1000 - (job.started ?? 0))
                  : (job.finished == null ? '' : fmtRelative(job.finished!)),
              valueColor: job.error != null ? c.danger : null,
            ),
            if (!job.running && job.started != null && job.finished != null)
              Row2('Dauer', fmtDuration(job.finished! - job.started!)),
            if (job.error != null)
              Padding(
                padding: const EdgeInsets.all(16),
                child: Text(job.error!, style: context.t.bodyMedium!.copyWith(color: c.danger)),
              ),
          ],
        ),
        if (job.steps.isNotEmpty)
          Section(
            title: 'Schritte',
            children: [
              for (final (i, s) in job.steps.indexed)
                StepTile(s, failed: job.error != null && i == job.steps.length - 1),
            ],
          ),
        Padding(
          padding: const EdgeInsets.fromLTRB(32, 0, 32, 8),
          child: Row(
            children: [
              Expanded(child: Text('PROTOKOLL', style: context.t.labelSmall!.copyWith(letterSpacing: .4))),
              if (skipped > 0)
                TextButton(
                  onPressed: () => setState(() => _all = !_all),
                  child: Text(_all ? 'Übersprungene ausblenden' : '$skipped übersprungene zeigen'),
                ),
            ],
          ),
        ),
        Section(children: [for (final e in entries) _EntryTile(e)]),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 32),
          child: Text(
            'Neueste oben. Übersprungene Songs versucht der Server nach 14 Tagen erneut; '
            'eingeordnet werden sie bis dahin über ihre Tags.',
            style: context.t.bodySmall,
          ),
        ),
      ],
    );
  }
}

class _EntryTile extends StatelessWidget {
  final LogEntry e;
  const _EntryTile(this.e);

  @override
  Widget build(BuildContext context) {
    final c = context.c;
    final (icon, color) = switch (e.kind) {
      'step' => (Icons.arrow_forward, c.tintText),
      'warn' => (Icons.warning_amber_rounded, _warn),
      'skip' => (Icons.redo, c.tertiary),
      'error' => (Icons.error_outline, c.danger),
      _ => (Icons.circle, c.tertiary),
    };
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 20,
            child: Padding(
              padding: EdgeInsets.only(top: e.kind == 'info' ? 7 : 1),
              child: Icon(icon, size: e.kind == 'info' ? 6 : 16, color: color),
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  e.text,
                  style: e.kind == 'step'
                      ? context.t.bodyLarge!.copyWith(fontWeight: FontWeight.w600)
                      : context.t.bodyMedium!.copyWith(color: e.kind == 'error' ? c.danger : null),
                ),
                if (e.detail != null) Text(e.detail!, style: context.t.bodySmall),
              ],
            ),
          ),
          const SizedBox(width: 8),
          Text(
            fmtClock(e.t),
            style: context.t.labelMedium!.copyWith(color: c.tertiary, fontFeatures: tabular),
          ),
        ],
      ),
    );
  }
}

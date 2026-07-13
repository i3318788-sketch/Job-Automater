"""Run the ATS checker against a CV file and a job description.

Usage:
    python manage.py check_cv_ats --cv path/to/cv.pdf --job path/to/job.txt
    python manage.py check_cv_ats --cv cv.docx --job job.txt --title "SEO Executive"
    python manage.py check_cv_ats --cv cv.docx            # format check only
    python manage.py check_cv_ats --cv cv.docx --job job.txt --json
"""
import json
import os
import sys

from django.core.management.base import BaseCommand, CommandError

from jobs.services.ats_checker import ATSChecker, check_cv_format, extract_job_requirements
from jobs.utils import extract_text_from_docx, extract_text_from_pdf


class Command(BaseCommand):
    help = 'Run the full ATS check on a CV file against a job description.'

    def add_arguments(self, parser):
        parser.add_argument('--cv', required=True, help='Path to a PDF or DOCX CV.')
        parser.add_argument(
            '--job', default=None,
            help='Path to a job description text file. Omit for a format-only check.',
        )
        parser.add_argument('--title', default='', help='Target job title.')
        parser.add_argument('--location', default='', help='Job location.')
        parser.add_argument(
            '--json', action='store_true', dest='as_json',
            help='Print the raw report as JSON instead of a formatted summary.',
        )

    # -- input ---------------------------------------------------------------

    def _read_cv(self, path):
        if not os.path.exists(path):
            raise CommandError(f'CV file not found: {path}')
        ext = os.path.splitext(path)[1].lower()
        with open(path, 'rb') as fh:
            if ext == '.pdf':
                return extract_text_from_pdf(fh)
            if ext == '.docx':
                return extract_text_from_docx(fh)
        raise CommandError(f'Unsupported CV type "{ext}". Use a PDF or DOCX file.')

    def _read_job(self, path):
        if not path:
            return ''
        if not os.path.exists(path):
            raise CommandError(f'Job description file not found: {path}')
        with open(path, 'r', encoding='utf-8') as fh:
            return fh.read()

    # -- output --------------------------------------------------------------

    def _verdict_style(self, score):
        if score >= 90:
            return self.style.SUCCESS
        if score >= 75:
            return self.style.WARNING
        return self.style.ERROR

    def _rule(self, title=''):
        self.stdout.write('')
        self.stdout.write(self.style.HTTP_INFO(f'-- {title} '.ljust(72, '-')))

    def _check_line(self, label, passed, detail=''):
        mark = self.style.SUCCESS('PASS') if passed else self.style.ERROR('FAIL')
        self.stdout.write(f'  [{mark}] {label}' + (f'  - {detail}' if detail else ''))

    def handle(self, *args, **options):
        # The report text uses typographic punctuation; a Windows console defaults
        # to cp1252 and would raise on it.
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, OSError):
            pass

        cv_path = options['cv']
        cv_text = self._read_cv(cv_path)
        job_text = self._read_job(options['job'])

        if not job_text:
            report = check_cv_format(cv_text, file_path=cv_path)
            if options['as_json']:
                self.stdout.write(json.dumps(report, indent=2, default=str))
                return
            self._print_format_only(report, cv_text)
            return

        requirements = extract_job_requirements(
            job_text, options['title'], options['location']
        )
        checker = ATSChecker(cv_text, job_text, requirements, file_path=cv_path)
        report = checker.get_detailed_report()

        if options['as_json']:
            self.stdout.write(json.dumps(report, indent=2, default=str))
            return
        self._print_report(report, cv_text)

    # -- renderers -----------------------------------------------------------

    def _print_format_only(self, phase1, cv_text):
        self._rule('PHASE 1  - PARSING & FORMATTING')
        self.stdout.write(f'  Characters extracted: {len(cv_text)}')
        self._check_line('Machine-readable', phase1['file_format_ok'],
                         phase1.get('file_format'))
        self._check_line('Single-column layout', phase1['layout'] == 'single-column',
                         phase1['layout'])
        self._check_line('No prohibited elements', not phase1['prohibited_elements'],
                         ', '.join(phase1['prohibited_elements']) or 'none found')
        self._check_line('Standard section headers', not phase1['missing_headers'],
                         'missing: ' + ', '.join(phase1['missing_headers'])
                         if phase1['missing_headers'] else
                         ', '.join(phase1['standard_headers']))
        self._check_line('Standard fonts', not phase1['font_issues'],
                         ', '.join(phase1['font_issues']) or 'ok')

        verdict = 'READABLE' if phase1['pass'] else 'WOULD BE REJECTED'
        style = self.style.SUCCESS if phase1['pass'] else self.style.ERROR
        self.stdout.write('')
        self.stdout.write(style(f'  Verdict: {verdict}'))
        self._print_recommendations(phase1.get('recommendations', []))
        self.stdout.write('')
        self.stdout.write(self.style.HTTP_INFO(
            '  (No job description given  - pass --job for the full 7-phase check.)'
        ))

    def _print_report(self, report, cv_text):
        phases = report['phases']
        p1 = phases['phase1_parsing']
        p2 = phases['phase2_knockout']
        p3 = phases['phase3_keyword']
        p4 = phases['phase4_context']
        p5 = phases['phase5_experience']
        p6 = phases['phase6_education']

        # Headline
        score = report['overall_score']
        self.stdout.write('')
        self.stdout.write(self._verdict_style(score)(
            f'  ATS SCORE: {score}/100   (threshold {report["threshold"]})'
        ))
        if report['rejected']:
            self.stdout.write(self.style.ERROR('  STATUS: ATS REJECTED  - hard filter failed'))
        elif report['pass']:
            self.stdout.write(self.style.SUCCESS('  STATUS: PASS'))
        else:
            self.stdout.write(self.style.WARNING('  STATUS: BELOW THRESHOLD'))

        if report['rejection_reasons']:
            self.stdout.write('')
            for reason in report['rejection_reasons']:
                self.stdout.write(self.style.ERROR(f'  x {reason}'))

        # Phase 1
        self._rule('PHASE 1  - PARSING & FORMATTING (hard filter)')
        if p1.get('file_checks_skipped'):
            self.stdout.write(self.style.WARNING(
                '  File-level checks skipped (install pdfplumber for PDF layout checks).'
            ))
        self._check_line('Machine-readable', p1['file_format_ok'], p1.get('file_format'))
        self._check_line('Single-column layout', p1['layout'] == 'single-column', p1['layout'])
        self._check_line('No prohibited elements', not p1['prohibited_elements'],
                         ', '.join(p1['prohibited_elements']) or 'none found')
        self._check_line('Standard section headers', not p1['missing_headers'],
                         'missing: ' + ', '.join(p1['missing_headers'])
                         if p1['missing_headers'] else ', '.join(p1['standard_headers']))
        self._check_line('Standard fonts', not p1['font_issues'],
                         ', '.join(p1['font_issues']) or 'ok')

        # Phase 2
        self._rule('PHASE 2  - KNOCK-OUT FILTERS (hard filter)')
        checks = [
            ('Years of experience', p2['experience_years'],
             lambda c: f'requires {c["required"]}, CV shows {c["found"]}'),
            ('Education level', p2['education'],
             lambda c: f'requires {c["required"]}, CV shows {c["found"] or "none"}'),
            ('Certifications', p2['certifications'],
             lambda c: f'requires {", ".join(c["required"])}; missing '
                       f'{", ".join(c["missing"]) or "none"}'),
            ('Minimum GPA', p2['gpa'],
             lambda c: f'requires {c["required"]}, CV shows {c["found"]}'),
            ('Location', p2['location'],
             lambda c: f'role in {c["required"]}, CV shows {c["found"] or "unknown"}'),
            ('Work authorisation', p2['work_authorization'],
             lambda c: f'job wants {c["required"]}, CV states {c["found"] or "nothing"}'),
        ]
        for label, check, describe in checks:
            if check.get('unverifiable'):
                # The job does require this, but the CV gives nothing to check it
                # against — which is never grounds for a knock-out.
                self.stdout.write(
                    f'  [{self.style.WARNING("WARN")}] {label} - required, but nothing '
                    f'on the CV to verify it against (not treated as a knock-out)'
                )
            elif check.get('skipped'):
                self.stdout.write(
                    f'  [{self.style.HTTP_INFO("SKIP")}] {label} - not required by this job'
                )
            else:
                self._check_line(label, check['pass'], describe(check))

        # Phase 3
        self._rule('PHASE 3  - KEYWORD MATCHING')
        self.stdout.write(f'  Score: {p3["score"]}/100')
        self.stdout.write(
            f'  Matched {len(p3.get("matched_keywords", []))} of {p3["total_keywords"]} '
            f'keywords ({p3["match_percentage"]}%), {p3["exact_matches"]} exact.'
        )
        if p3['hard_skills_found']:
            self.stdout.write(self.style.SUCCESS(
                '  Hard skills found:   ' + ', '.join(p3['hard_skills_found'][:12])
            ))
        if p3['hard_skills_missing']:
            self.stdout.write(self.style.ERROR(
                '  Hard skills MISSING: ' + ', '.join(p3['hard_skills_missing'][:12])
            ))
        if p3['soft_skills_found']:
            self.stdout.write('  Soft skills found:   ' + ', '.join(p3['soft_skills_found'][:8]))
        if p3['keyword_stuffing']:
            self.stdout.write(self.style.WARNING(
                '  Keyword stuffing detected: ' + ', '.join(p3['stuffed_keywords'])
            ))
        for u in p3.get('underused_keywords', [])[:5]:
            self.stdout.write(self.style.WARNING(
                f'  Underused: "{u["term"]}" - job mentions it {u["jd_count"]}x, '
                f'your CV {u["cv_count"]}x (aim for {u["expected"]})'
            ))

        # Phase 4
        self._rule('PHASE 4  - CONTEXT & PROXIMITY')
        self.stdout.write(f'  Score: {p4["score"]}/100')
        self.stdout.write(f'  Job title match:  {p4["job_title_match"]}/100')
        self.stdout.write(f'  Proximity (skills evidenced in experience): {p4["proximity_score"]}/100')
        self.stdout.write(f'  Recency (skills in 2 most recent roles):    {p4["recency_score"]}/100')
        self.stdout.write(f'  Quantification (bullets with numbers):      {p4["quantification_score"]}/100')

        # Phase 5
        self._rule('PHASE 5  - EXPERIENCE & CHRONOLOGY')
        self.stdout.write(f'  Score: {p5["score"]}/100')
        self.stdout.write(f'  Roles found: {p5["roles_found"]} | '
                          f'total experience {p5["total_experience_years"]} yrs | '
                          f'avg tenure {p5["avg_tenure_years"]} yrs')
        self._check_line('Reverse-chronological', p5['reverse_chronological'])
        self._check_line('No unexplained gaps', not p5['gaps'],
                         '; '.join(f'{g["from"]}->{g["to"]} ({g["months"]} mo)'
                                   for g in p5['gaps']) or 'none')
        self._check_line('No job-hopping', not p5['job_hopping'])

        # Phase 6
        self._rule('PHASE 6  - EDUCATION')
        self.stdout.write(f'  Score: {p6["score"]}/100')
        self.stdout.write(f'  Degree: {p6["degree_hierarchy"] or "none detected"} | '
                          f'GPA/classification: {p6["gpa"] or "-"} | '
                          f'graduated: {p6["graduation_year"] or "-"}')

        # Phase 7
        self._rule('CATEGORY BREAKDOWN')
        for name, cat in report['categories'].items():
            label = name.replace('_', ' ').title()
            weight = f'{cat["weight"]:.0%}' if cat['weight'] else '   -'
            bar = '#' * (cat['score'] // 5)
            self.stdout.write(
                f'  {label:<22} {cat["score"]:>3}/100  (weight {weight:>4})  {bar}'
            )
            for issue in cat.get('issues', [])[:3] + cat.get('density_issues', [])[:3]:
                self.stdout.write(f'      - {issue}')

        self._print_recommendations(report['recommendations'])
        self.stdout.write('')

    def _print_recommendations(self, recommendations):
        if not recommendations:
            return
        self._rule('RECOMMENDATIONS')
        for i, rec in enumerate(recommendations, start=1):
            self.stdout.write(f'  {i}. {rec}')

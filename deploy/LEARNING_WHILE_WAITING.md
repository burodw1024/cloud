# Learn while a query runs

The workspace now includes an educational loading state: short, optional lessons
about agentic AI and report usage while the main request is pending. This UX
pattern is also called microlearning during waiting or perceived-performance
design. It does not reduce actual backend latency or pretend to report progress.

The local card appears after 700 ms, avoiding a flash for quick responses. Eight
English/Bangla lessons cover agent/tool workflows, retrieval-augmented generation,
well-defined tasks, report dates and scope,
ranking levels, access controls, interpreting top-N results, and avoiding duplicate
submissions. They are general learning material, not additional capabilities or
an authorization grant. The example fragments need the user's actual dates and
permitted identifiers before use.

Users can switch language, move between lessons, pause rotation or hide lessons.
Language and visibility preferences are stored locally in their browser. With no
language preference, Bangla questions select Bangla lessons. Tips rotate every
12 seconds, pause when a learning control receives focus, and stop rotating when
the tab is hidden. Reduced-motion users start with rotation paused. There is no
automatic announcement of rotating lessons to assistive technology.

The existing request status and actual elapsed time remain visible. There are no
fake percentages, completion estimates or claimed live processing stages. The
learning card's conceptual workflow is explicitly general guidance. Timers stop
and the card disappears on success, clarification, denied access, proxy timeout
or network failure. A quick response cancels the pending reveal. If focus was
inside the card, completion moves it to the results heading.

Content is bundled in static assets; tips never call a model, dispatch a tool,
submit the question again or make third-party requests. Loading the page fetches
the small local CSS/JS assets as usual. Existing duplicate-submit prevention,
authorization, report handling and error messages remain intact.

Deploy with the existing on-prem launcher after pulling main. No database
migration, signing configuration or NiFi change is needed for this feature.

Validation: all 384 application tests passed. Headless Chromium checked delayed
appearance, rotation/pause, next/previous, language and hide/show controls,
mobile layout, reduced-motion behavior and cleanup on success, access denial,
HTTP 504, network failure and a fast response. Learning interactions dispatched
no additional report requests. These checks used mocked report responses, not
live NiFi/Oracle queries.

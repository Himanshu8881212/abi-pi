# Add an object type, an action and a form card in 5 minutes

An **object type** is a business thing your model already has a keyed table for: a café, a customer. An **action** is
something a person does to one object through a form. A **form card** puts that form on a report page. All three are
project files, so they are in Git, in the project export, and checked like every other file.

The example uses the Brewline demo project (`bench/brewline.py`): the table `cafes` is keyed by `cafe_id` and has the
columns `cafe_name`, `region` and `seats`; `revenue` is a measure.

## 1. The object type and the action: `model/ontology.json`

```json
{
  "$schema": "https://schema.abi.invalid/v1/ontology.json",
  "objects": [
    {"id": "cafe", "table": "cafes", "name": "Café", "plural": "Cafés", "title": "cafe_name",
     "description": "One Brewline shop.",
     "properties": [{"column": "region", "meaning": "category"}, {"column": "seats", "meaning": "quantity"}],
     "measures": ["revenue"]}
  ],
  "actions": [
    {"id": "set_seats", "object": "cafe", "name": "Change the seats",
     "description": "Corrects how many seats a café has.",
     "role": "editor", "confirm": true,
     "inputs": {"type": "object", "required": ["seats"], "additionalProperties": false,
                "properties": {"seats": {"type": "integer", "minimum": 0, "maximum": 500, "title": "Seats",
                                         "description": "The number of seats after the change."}}},
     "checks": [{"value": "{{ data.inputs.seats }}", "op": ">=", "to": 10, "message": "A café has at least 10 seats."}],
     "writes": {"table": "cafes", "set": {"seats": "{{ data.inputs.seats }}"}}}
  ]
}
```

What each part does:

- `table` is a model table with a key; the key identifies an object. `title` is the column shown as its name.
- `properties` lists only the columns that need a name or a meaning; every other visible column shows too. A `category`
  becomes a filter on the list.
- `measures` are measure IDs: the list and the object page show them for each object.
- `inputs` is a JSON Schema of type object. Each property is one field of the form: its `title` is the label, its
  `description` the hint, its type the input (number, date, choice for an `enum`, checkbox for a boolean). The fields
  show in the order `required` lists them, then the others.
- `checks` are tested when the form is sent. A check that doesn't hold stops the action and its `message` shows in the form.
- `writes` sets columns of the object's row (or of a keyed table one-to-one with it). It is one logged edit that can be
  undone under Data › Edits. `confirm` shows the old and new values first; the person confirms.
- `role` is who may run it through the API or an agent: `viewer`, `editor` (the default) or `owner`. In the app,
  actions run in Developer mode only, so by Editors and Owners (see "Who can use a form" below).

## 2. The form card: a visual of type `form` in `reports/cafe-visits.json`

```json
{
  "$schema": "https://schema.abi.invalid/v1/report.json",
  "id": "cafe-visits", "name": "Café visits",
  "pages": [
    {"id": "main", "name": "Main", "visuals": [
      {"id": "by-cafe", "type": "table", "title": "Revenue by café",
       "query": {"measures": ["revenue"], "dimensions": ["cafes.cafe_name"]}, "layout": {"x": 0, "y": 0, "w": 7, "h": 6}},
      {"id": "seats", "type": "form", "title": "Change a café's seats",
       "form": {"action": "set_seats"}, "layout": {"x": 7, "y": 0, "w": 5, "h": 6}}
    ]}
  ]
}
```

- `form.action` is the action's `id`. The person picks the café in the card.
- To bind the card to one café, add its key: `"form": {"action": "set_seats", "key": ["CF001"]}`.
- A form card has no `query` and no `text`.

## 3. Save them

Either way ABI checks the files first and names the file and the field of anything wrong, for example
`reports/cafe-visits.json $.pages[0].visuals[1].form.action: Unknown action type 'set_seat'.`

- **In chat:** paste the JSON and ask the agent to save it. It calls `save_ontology` and `save_report`; you confirm the
  ontology change in the dialog.
- **As files:** put the two files into the project folder under `workspaces/`, then run
  `scripts/abi.py cli diff <project>` (the checks, dry run) and `scripts/abi.py cli apply <project>`.

## 4. Use it

- **Data Model › Objects** shows the types and how they connect. Open **Cafés** for the list; a row opens the café's
  page with its properties, its measures, what it connects to and its history. **Change the seats** is the button at
  the top.
- **Café visits** (Reports) shows the form card beside the table. After the action ran, the page's numbers refresh.
  A click on a café in the table filters the report by it, as every click does; **Open**, beside the filter, shows
  that café's page, and Back returns to the report.

## An alert per object

A flow can check a condition for each object and alert the people linked to it: one alert per café, sent to that
café's manager. Say each café's manager is kept in an input table `cafe_contacts` (keyed by `cafe_id`, with a
relationship onto `cafes`) and the café type lists it as a property with a meaning:
`{"column": "cafe_contacts.manager", "name": "Manager", "meaning": "email"}`. The flow is `flows/cafe_revenue_low.json`:

```jsonc
{
  "$schema": "https://schema.abi.invalid/v1/flow.json",
  "id": "cafe_revenue_low", "name": "Café revenue is low", "enabled": true,
  "trigger": {"type": "data_condition",
              "query": {"measures": ["revenue"]}, "measure": "revenue", "op": "<", "value": 5000,
              "period": "calendar.month", "skip_latest": true,
              "per": "cafe", "recipients": {"property": "cafe_contacts.manager"}},
  "steps": [
    {"id": "inbox", "type": "action", "action": "post_message",
     "input": {"channel": "inbox", "text": "Revenue at {{ data.trigger.title }} was low last month.",
               "data": {"measures": ["revenue"], "dimensions": ["cafes.cafe_name"]}}},
    {"id": "email", "type": "action", "action": "send_email",
     "when": {"value": "{{ data.trigger.recipients }}", "op": "!=", "to": []},
     "input": {"to": "{{ data.trigger.recipients }}", "subject": "Revenue at {{ data.trigger.title }} was low",
               "body": "Last month's figure is below and attached.",
               "data": {"measures": ["revenue"], "dimensions": ["cafes.cafe_name"]}}}
  ]
}
```

- `per` is the object type's `id`. ABI checks the condition for each café and starts one run for each café whose row
  **starts** to hold; a café that keeps holding gets no second alert until it stops and starts again (and
  `cooldown_minutes` has passed, a day by default). At most 50 cafés start in one check; if more start at once, the
  flow's owner gets one Inbox message listing the others by name.
- A run reads its café as `{{ data.trigger.title }}`, `{{ data.trigger.key }}` and `{{ data.trigger.object.<column> }}`;
  a property from a one-to-one table as `{{ data.trigger.object.cafe_contacts.manager }}`.
- `recipients.property` is a property with the meaning `email` or `person`. Its value names a **project member**, by
  their account's email or name. With `"link": "<link id>"` it is read from the objects that link reaches instead
  (a café's employees). Leave `recipients` out and the Inbox post goes to everyone in the project who can see the café.
- Only project members are ever sent to, and each only when they can see that café themselves. They get their own
  rows of `data`, filtered to the café. An address in the data never becomes a recipient by itself. Anyone else is
  skipped: the run's trace (`flow_runs`) says who and why, and the flow's owner gets an Inbox message naming them and
  what to do (a workspace owner adds the person under Users & access with that email, or you change their access or
  the value). When nobody can be reached the run ends as stopped, and starts no cooldown.
- The condition is checked with the flow owner's access. For a recipient whose rows are restricted it is checked
  again with their own access, for that café: they get the alert only when it holds in the rows they may read
  themselves, with those rows. Otherwise they are skipped (the step's trace names them) and learn nothing. That
  second check uses the condition the run started with, on the data as it is when the alert is sent.
- `{{ data.trigger.recipients }}` is those members' account emails. When none of them has one, the email step's
  `when` (as above) skips it; without that `when` the run stops at the step, with the reason, and later steps don't
  run. The first email to a domain the project hasn't
  mailed before still waits for a person: the email step fails with "needs confirmation" and says what to do. To
  confirm, send one email to an address of that domain yourself (ask the agent, or `send_email`) and confirm ABI's
  question; the next alert then goes out.
- What a per-object alert may do, and nothing else: steps `refresh`, `agent`, `snapshot` and `condition` (on a
  measure), and the actions `send_email`, `post_message` to channel `inbox`, `refresh`, `run_agent`,
  `report_snapshot`, `semantic_query`, `get_object`, `list_objects` and `visual_data`. Saving refuses everything else
  with the file, the field and the fix: a Slack, Teams or webhook channel, `notify`, an approval, a tool step,
  `run_flow`, a report, docs or tool result attached to a send, an "answer changed" check, and an object type on a
  live table (its own, a one-to-one property's or the recipients link's). Inside the run every published API tool is
  refused too, reading ones as well, and so is `propose_edit` (an agent's tool call included). It can't be run by hand. A failed run is reported in the owner's own Inbox, not in the project's alerts.
- In the app, **Monitor › New monitor** makes the same flow: choose "each café" under Check and who it is for under
  Send to. On the Monitor card and in the Inbox, a café's name opens its page.

## A prediction on an object

An ML model can predict something for each object of a type: the revenue to expect from a café, how likely a customer
is to leave. Add `object` to the model and its prediction becomes a property of the type. The example predicts each
café's revenue from its seats, format and region. First a dataset with one row per café, `datasets[]` in
`model/datasets.json`:

```json dataset
{"id": "cafe_features", "name": "Café features",
 "query": {"measures": ["revenue"], "dimensions": ["cafes.cafe_id", "cafes.seats", "cafes.format", "cafes.region"]}}
```

Then the model, `ml/expected_revenue.json`:

```json ml
{
  "$schema": "https://schema.abi.invalid/v1/ml.json",
  "id": "expected_revenue", "name": "Expected revenue",
  "task": "regression", "dataset": "cafe_features", "object": "cafe",
  "target": "revenue", "features": ["cafes.seats", "cafes.format", "cafes.region"]
}
```

- `object` is the object type's `id`; the type has one key column. The dataset's dimensions are that key
  (`cafes.cafe_id`) and other columns of the type's table only, and no date columns: one row, so one prediction, per café.
- A feature from another type is a measure reached through a link: name the link in the dataset query's `links`
  (the number of customers who visited each café: `"links": ["visited"]`).
- Train it, then score it (in chat, or **Train** and **Score** on the model's card under ML models). Scoring adds the
  property to the type and every café's page shows **Expected revenue**. The name is the model's `name`; rename it or
  change its meaning in the ontology and later scorings keep your change.
- What the property is depends on the `task`: `regression` gives the number, `clustering` the group, `anomaly` a
  Yes/No and a score. A `forecast` keeps the dataset's date columns and `time`, and forecasts one series per café:
  its total ahead shows among the café's measures. Date columns are refused on every other task, so an anomaly per
  café and season is not possible in 1.2.6.
- A `classification` with two outcomes gives a probability. For a yes/no (or 0/1) target it is the probability of
  yes. For any other pair, say which outcome it is for: `"positive": "Churned"`. Without `positive` the property is
  the predicted outcome and there is no probability; saving the model answers with a note naming the two values.
  A model has one property: once it makes a probability, that takes the place of the predicted outcome.
- Remove a property from the type and later scorings leave it out. Take `object` off the model, or delete the
  model, and its property leaves the type with it. A deleted model's predictions table stays in the model, hidden
  from restricted Viewers until it gets a row rule of its own (Security); so is any model table named `ml_<x>`
  that has no ML model `<x>`.
- A person sees a prediction only when they see its object. Where the model learned from rows a person's row rules
  don't reach through the type itself (other types' rows, reached through a link), restricted Viewers see no
  prediction at all until the predictions table gets a row rule of its own (Security). Such a prediction reads
  "hidden for your access" on the object's page, not as an empty value.
- The predictions table copies the dataset's dimension columns. Where a column is hidden from Viewers (Security),
  the table is hidden from them too until its Owner gives it a column rule of its own; then only its copy of a
  hidden key stays hidden automatically. List the other copied columns in that rule yourself.
- An alert on it is "An alert per object" above with the prediction's measure: `expected_revenue_prediction` here,
  `<id>_probability` for a probability. A flow with trigger `{"type": "after_refresh", "tables": ["ml_expected_revenue"]}`
  runs after each scoring.
- Wrong references are refused when the model is saved, with the file, the field and the fix, for example
  `ml/expected_revenue.json $.object: Dataset 'cafe_features' isn't grouped by cafes.cafe_id, the key of object type 'cafe'.`
  A dataset grouped by a column of another table is refused the same way.

## Who can use a form

In 1.2.6 the app's action buttons and form cards are for **Editors and Owners, in Developer mode**. Viewer mode and
the published version are read-only: they show objects and their pages, but no action buttons and no form cards, and
Publish leaves action types and form cards out of the published copy. A `role: viewer` action can still be run by a
Viewer through the API or an agent.

The published version carries the loaded data, not what actions and edits wrote in the working project: there, a
property an action set (a manager, a visit reason) can read "—". The working project, in Developer, shows it.

## The same thing twice (entity resolution)

Two systems often hold the same customer under two IDs. Add `resolve` to the object type and ABI proposes which
records are the same one; a person decides. The example is a `customers` table keyed by `customer_id` with the columns
`name`, `vat`, `email`, `postcode` and `source_system`:

```json resolve
{"id": "customer", "table": "customers", "name": "Customer", "plural": "Customers", "title": "name",
 "resolve": {
   "exact": [{"column": "vat", "as": "id"}, {"column": "email", "as": "email"}],
   "fuzzy": [{"column": "name", "threshold": 0.9}],
   "block": ["postcode"],
   "source": "source_system"}}
```

- `exact` columns are compared after normalising: `id` keeps letters and digits (a VAT number), `email` trims and
  lower-cases. Two `phone` values are the same when, without spaces, signs, "(0)" and leading zeros, the shorter one (7
  digits or more) ends the longer one: a number with and without its country code. `fuzzy` columns are text: `threshold` is how alike two values must be,
  from 0 to 1 (`method`: `jaro_winkler`, the default, or `levenshtein`).
- Two records are proposed as the same when an exact column is equal, or when every fuzzy column reaches its threshold
  and no exact column differs. A pair with one exact column the same and another different (the same email under two
  VAT numbers) is still proposed, after the clear ones, and its evidence says "different VAT number". ABI never compares every pair: only records that share an exact value, a `block`
  value (the same postcode) or the first 4 letters of the first fuzzy column. A value shared by more than 200
  records (an empty-like postcode) is skipped, and the answer says so.
- Nothing is merged by itself. The type's list shows **Possible duplicates** to Editors and Owners: "These 2 customers
  look like the same one", with why. **Same customer** or **Not the same** is the person's decision, confirmed; in chat
  the agent proposes (`find_same`) and ABI asks the person before `decide_same` does anything.
- After **Same customer** the group is one customer under one stable ID (the key of its first record). Reports,
  measures, the customer's page, flows and ML models count it once and show its orders together. The Data grid still
  shows the source rows. A merged customer's page lists its records; **Take out** splits one off again.
- Every decision is listed under **Decided** with who and when, and **Undo** takes it back. Decisions are kept in the
  input table `same_as_customers` (hidden from the model; one logged edit batch each). Only `decide_same` writes it:
  every other write-back, load and step into it is refused, Data › Edits doesn't offer it, a flow can't undo a
  decision and an agent's undo asks the person. Undo goes newest first per customer: a decision is undone once no
  newer decision in force is about the same records or the customers they are in now ("A newer decision builds on
  this one: undo … first"); until then Decided shows it without the Undo button. Decided reads the newest 1,000
  history entries: older decisions stay in force but are no longer listed.
- **Not the same** holds until it is undone: the two are not proposed together again, a proposed group never contains
  both, and a merge that would make them one, directly or through other records, is refused with their names.
- The group keeps the ID it had; when two groups merge, the lower ID stays. The ID moves to the first of the rest when
  the record that carries it is taken out, and the group shows under the first remaining record when that record
  disappears from the source.
- A table with one row per record (predictions, an input table keyed like the type) shows the row of the record the
  group shows under. Write-back (the Data grid, an action's write) edits a source record, never the group.
- A merged customer shows the values of the record it shows under. The other records' own values (another spelling of
  the name, another address) are no longer searched, filtered or matched against new records; they stay in the Data grid.
- **Take out** says the record is not that customer: it is not proposed for it or merged into it again until the
  take-out is undone under Decided.
- Row-level security: a restricted Viewer sees a merged customer only when they may see at least one of its records,
  and then only those records' values, orders and counts. Where the record carrying the group's ID is hidden from
  them, the customer shows under the first record they see. Proposals and decisions are for Editors and Owners; a
  restricted Viewer reads nothing of the decisions table. On a phone the app is read-only: it shows the merged list
  and no **Possible duplicates** card.
- Decisions are data of one environment, like edits: a promote, a project export or import and the published version
  carry the table's definition and none of its rows. So people reading the published version (Viewer mode, a phone)
  keep seeing merged customers separately; the merge confirmation says so when a published version exists. What
  Publish carries is decided in 1.2.7. In a production environment `decide_same` works once the table
  came by a promote (make the first decision in dev, promote, then decide in production).
- Speed: while a type has at least one merged record, queries on its table and on every table pointing at it read
  through views instead of ABI's query copies and shared partials. One measurement on pve1 (100,000 companies,
  1,000,000 orders, sales by postcode, first read of a snapshot, query copies built): 0.18 s before any decision,
  0.39 to 0.44 s with one merge in force (about twice as long), 0.20 to 0.22 s after it was undone
  (`test_er_blocking_scales` records it on every run). After the last merge is undone, queries stop reading through
  the views at once. The fast copies are used again at once when they are still there; the background worker (it
  checks every minute) drops the copy of a table read through the views and builds it again after the undo. Bigger tables and
  wider dimensions were not measured.
- Wrong references are refused when the ontology is saved, with the file, the field and the fix, for example
  `model/ontology.json $.objects[0].resolve.fuzzy[0].column: Object type 'customer': table 'customers' has no column 'nam'.`
- Limits in 1.2.6: the records of a type live in one table (append the systems' tables first, in Data), that table
  has one key column, and it is a loaded table, not a live one.

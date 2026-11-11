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

## Who can use a form

In 1.2.6 the app's action buttons and form cards are for **Editors and Owners, in Developer mode**. Viewer mode and
the published version are read-only: they show objects and their pages, but no action buttons and no form cards, and
Publish leaves action types and form cards out of the published copy. A `role: viewer` action can still be run by a
Viewer through the API or an agent. Forms for Viewers in the published app are planned for 1.2.7.

The published version carries the loaded data, not what actions and edits wrote in the working project: there, a
property an action set (a manager, a visit reason) can read "—". The working project, in Developer, shows it.

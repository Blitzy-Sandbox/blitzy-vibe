from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

from vibe.cli.textual_ui.widgets.no_markup_static import NoMarkupStatic

if TYPE_CHECKING:
    from vibe.cli.textual_ui.widgets.question_app import QuestionApp
    from vibe.core.tools.builtins.ask_user_question import Choice

__all__ = ["AnswerManager", "QuestionRenderer", "SelectionHelper"]


class SelectionHelper:
    """Handles selection, toggle, and question navigation logic for QuestionApp."""

    def __init__(self, app: QuestionApp) -> None:
        self._app = app

    def handle_multi_select_action(self) -> None:
        """Handle Enter key in multi-select mode: toggle option or submit."""
        if self._app._is_submit_selected:
            self._app._answer_manager.save_current_answer()
            self.advance_or_submit()
        elif self._app._is_other_selected:
            if self._app.other_input:
                self._app.other_input.focus()
        else:
            self.toggle_selection(self._app.selected_option)

    def handle_single_select_action(self) -> None:
        """Handle Enter key in single-select mode: select and advance."""
        if self._app._is_other_selected:
            if self._app.other_input:
                other_text = self._app.other_texts.get(
                    self._app.current_question_idx, ""
                ).strip()
                if other_text:
                    self._app._answer_manager.save_current_answer()
                    self.advance_or_submit()
                else:
                    self._app.other_input.focus()
        else:
            self._app._answer_manager.save_current_answer()
            self.advance_or_submit()

    def toggle_selection(self, option_idx: int) -> None:
        """Toggle an option's selection state (multi-select only)."""
        selections = self._app.multi_selections.setdefault(
            self._app.current_question_idx, set()
        )
        if option_idx in selections:
            selections.discard(option_idx)
        else:
            selections.add(option_idx)
        self._app._question_renderer.update_display()

    def advance_or_submit(self) -> None:
        """Advance to next unanswered question or submit all."""
        if self._app._answer_manager.all_answered():
            self._app._submit_answers()
        else:
            new_idx = next(
                i
                for i in itertools.chain(
                    range(self._app.current_question_idx + 1, len(self._app.questions)),
                    range(self._app.current_question_idx),
                )
                if i not in self._app.answers
            )
            self.switch_question(new_idx)

    def switch_question(self, new_idx: int) -> None:
        """Switch to a different question by index."""
        self._app.current_question_idx = new_idx
        self._app.selected_option = 0

    def navigate_to_next_question(self) -> None:
        """Navigate to the next question in sequence."""
        if self._app._is_other_selected:
            other_text = self._app.other_texts.get(
                self._app.current_question_idx, ""
            ).strip()
            if not other_text:
                return
        new_idx = (self._app.current_question_idx + 1) % len(self._app.questions)
        self.switch_question(new_idx)

    def navigate_to_prev_question(self) -> None:
        """Navigate to the previous question in sequence."""
        new_idx = (self._app.current_question_idx - 1) % len(self._app.questions)
        self.switch_question(new_idx)

    def sync_other_selection_with_text(self) -> None:
        """Auto-select/deselect 'Other' option based on whether text is entered (multi-select only)."""
        if not self._app._current_question.multi_select or not self._app.other_input:
            return

        other_idx = len(self._app._current_question.options)
        selections = self._app.multi_selections.setdefault(
            self._app.current_question_idx, set()
        )
        has_text = bool(self._app.other_input.value.strip())

        if has_text and other_idx not in selections:
            selections.add(other_idx)
        elif not has_text and other_idx in selections:
            selections.discard(other_idx)


class AnswerManager:
    """Handles answer saving and retrieval for QuestionApp."""

    def __init__(self, app: QuestionApp) -> None:
        self._app = app

    def store_other_text(self) -> None:
        """Store the current other-input text for the active question."""
        if self._app.other_input:
            self._app.other_texts[self._app.current_question_idx] = (
                self._app.other_input.value
            )

    def get_other_text(self, idx: int) -> str:
        """Retrieve stored other-text for a given question index."""
        return self._app.other_texts.get(idx, "")

    def save_current_answer(self) -> None:
        """Save the current answer, dispatching to multi or single select."""
        if self._app._current_question.multi_select:
            self.save_multi_select_answer()
        else:
            self.save_single_select_answer()

    def save_multi_select_answer(self) -> None:
        """Save answer for multi-select question (combines all selected options)."""
        q = self._app._current_question
        idx = self._app.current_question_idx
        selections = self._app.multi_selections.get(idx, set())

        if not selections:
            return

        other_text = self._app.other_texts.get(idx, "").strip()
        answers = []
        has_other = False
        other_idx = len(q.options)

        for sel_idx in sorted(selections):
            if sel_idx < len(q.options):
                answers.append(q.options[sel_idx].label)
            elif sel_idx == other_idx and other_text:
                answers.append(other_text)
                has_other = True

        if answers:
            self._app.answers[idx] = (", ".join(answers), has_other)

    def save_single_select_answer(self) -> None:
        """Save answer for single-select question."""
        idx = self._app.current_question_idx

        if self._app._is_other_selected:
            other_text = self._app.other_texts.get(idx, "").strip()
            if other_text:
                self._app.answers[idx] = (other_text, True)
        else:
            self._app.answers[idx] = (
                self._app._current_question.options[self._app.selected_option].label,
                False,
            )

    def all_answered(self) -> bool:
        """Check if all questions have been answered."""
        return all(i in self._app.answers for i in range(len(self._app.questions)))


class QuestionRenderer:
    """Handles display and rendering for QuestionApp."""

    def __init__(self, app: QuestionApp) -> None:
        self._app = app

    def update_display(self) -> None:
        """Update all display elements for the current question."""
        self.update_tabs()
        self.update_title()
        self.update_options()
        self.update_other_row()
        self.update_submit()
        self.update_help()

    def update_tabs(self) -> None:
        """Update the question tab indicators."""
        if not self._app.tabs_widget or len(self._app.questions) <= 1:
            return
        tabs = []
        for i, question in enumerate(self._app.questions):
            header = question.header or f"Q{i + 1}"
            if i in self._app.answers:
                header += " ✓"
            if i == self._app.current_question_idx:
                tabs.append(f"[{header}]")
            else:
                tabs.append(f" {header} ")
        self._app.tabs_widget.update("  ".join(tabs))

    def update_title(self) -> None:
        """Update the question title text."""
        if self._app.title_widget:
            self._app.title_widget.update(self._app._current_question.question)

    def update_options(self) -> None:
        """Update the option widgets for the current question."""
        q = self._app._current_question
        options = q.options
        is_multi = q.multi_select
        multi_selected = self._app.multi_selections.get(
            self._app.current_question_idx, set()
        )

        for i, widget in enumerate(self._app.option_widgets):
            if i < len(options):
                is_focused = i == self._app.selected_option
                is_selected = i in multi_selected
                self.render_option(
                    widget, i, options[i], is_multi, is_focused, is_selected
                )
            else:
                widget.update("")
                widget.display = False

    def format_option_prefix(
        self, idx: int, is_focused: bool, is_multi: bool, is_selected: bool
    ) -> str:
        """Format the prefix for an option line (cursor + number + checkbox if multi)."""
        cursor = "› " if is_focused else "  "
        if is_multi:
            check = "[x]" if is_selected else "[ ]"
            return f"{cursor}{idx + 1}. {check} "
        return f"{cursor}{idx + 1}. "

    def render_option(
        self,
        widget: NoMarkupStatic,
        idx: int,
        opt: Choice,
        is_multi: bool,
        is_focused: bool,
        is_selected: bool,
    ) -> None:
        """Render a single option widget."""
        prefix = self.format_option_prefix(idx, is_focused, is_multi, is_selected)
        text = f"{prefix}{opt.label}"

        if opt.description:
            text += f" - {opt.description}"

        widget.update(text)
        widget.display = True
        widget.remove_class("question-option-selected")
        if is_focused:
            widget.add_class("question-option-selected")

    def update_other_row(self) -> None:
        """Update the 'Other' option row display."""
        if (
            not self._app.other_prefix
            or not self._app.other_input
            or not self._app.other_static
        ):
            return

        q = self._app._current_question
        is_multi = q.multi_select
        multi_selected = self._app.multi_selections.get(
            self._app.current_question_idx, set()
        )
        other_idx = len(self._app._current_question.options)
        is_focused = self._app._is_other_selected
        is_selected = other_idx in multi_selected

        prefix = self.format_option_prefix(other_idx, is_focused, is_multi, is_selected)
        self._app.other_prefix.update(prefix)

        stored_text = self._app.other_texts.get(self._app.current_question_idx, "")
        if self._app.other_input.value != stored_text:
            self._app.other_input.value = stored_text

        show_input = is_focused or bool(stored_text)

        self._app.other_input.display = show_input
        self._app.other_static.display = not show_input

        self._app.other_prefix.remove_class("question-option-selected")
        if is_focused:
            self._app.other_prefix.add_class("question-option-selected")

        if is_focused and show_input:
            self._app.other_input.focus()
        elif not is_focused and not self._app._is_submit_selected:
            self._app.focus()

    def update_submit(self) -> None:
        """Update the submit/next button display."""
        if not self._app.submit_widget:
            return

        q = self._app._current_question
        if not q.multi_select:
            self._app.submit_widget.display = False
            return

        self._app.submit_widget.display = True
        is_focused = self._app._is_submit_selected
        cursor = "› " if is_focused else "  "

        text = (
            "Submit"
            if len(set(self._app.answers.keys()) | {self._app.current_question_idx})
            == len(self._app.questions)
            else "Next"
        )
        self._app.submit_widget.update(f"{cursor}   {text} →")
        self._app.submit_widget.remove_class("question-option-selected")
        if is_focused:
            self._app.submit_widget.add_class("question-option-selected")
            self._app.focus()

    def update_help(self) -> None:
        """Update the help text display."""
        if not self._app.help_widget:
            return
        if self._app._current_question.multi_select:
            help_text = "↑↓ navigate  Enter toggle  Esc cancel"
        else:
            help_text = "↑↓ navigate  Enter select  Esc cancel"
        if len(self._app.questions) > 1:
            help_text = "←→ questions  " + help_text
        self._app.help_widget.update(help_text)

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Input

from vibe.cli.textual_ui.widgets.no_markup_static import NoMarkupStatic
from vibe.cli.textual_ui.widgets.question_app_helpers import (
    AnswerManager,
    QuestionRenderer,
    SelectionHelper,
)

if TYPE_CHECKING:
    from vibe.core.tools.builtins.ask_user_question import AskUserQuestionArgs, Question

from vibe.core.tools.builtins.ask_user_question import Answer

__all__ = ["QuestionApp"]


class QuestionApp(Container):
    MAX_OPTIONS: ClassVar[int] = 4

    can_focus = True
    can_focus_children = False

    current_question_idx: reactive[int] = reactive(0)
    selected_option: reactive[int] = reactive(0)

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False),
        Binding("down", "move_down", "Down", show=False),
        Binding("enter", "select", "Select", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    class Answered(Message):
        def __init__(self, answers: list[Answer]) -> None:
            super().__init__()
            self.answers = answers

    class Cancelled(Message):
        pass

    def __init__(self, args: AskUserQuestionArgs) -> None:
        super().__init__(id="question-app")
        self.args = args
        self.questions = args.questions

        self.answers: dict[int, tuple[str, bool]] = {}
        self.multi_selections: dict[int, set[int]] = {}
        self.other_texts: dict[int, str] = {}

        self.option_widgets: list[NoMarkupStatic] = []
        self.title_widget: NoMarkupStatic | None = None
        self.other_prefix: NoMarkupStatic | None = None
        self.other_input: Input | None = None
        self.other_static: NoMarkupStatic | None = None
        self.submit_widget: NoMarkupStatic | None = None
        self.help_widget: NoMarkupStatic | None = None
        self.tabs_widget: NoMarkupStatic | None = None

        # Compose helper classes via __init__ injection (R2)
        self._selection_helper = SelectionHelper(self)
        self._answer_manager = AnswerManager(self)
        self._question_renderer = QuestionRenderer(self)

    @property
    def _current_question(self) -> Question:
        return self.questions[self.current_question_idx]

    @property
    def _is_other_selected(self) -> bool:
        return self.selected_option == len(self._current_question.options)

    @property
    def _is_submit_selected(self) -> bool:
        return (
            self._current_question.multi_select
            and self.selected_option == len(self._current_question.options) + 1
        )

    @property
    def _total_options(self) -> int:
        """Total navigable options including Other and optional Submit."""
        return (
            len(self._current_question.options)
            + 1
            + (1 if self._current_question.multi_select else 0)
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="question-content"):
            if len(self.questions) > 1:
                self.tabs_widget = NoMarkupStatic("", classes="question-tabs")
                yield self.tabs_widget

            self.title_widget = NoMarkupStatic("", classes="question-title")
            yield self.title_widget

            for _ in range(self.MAX_OPTIONS):
                widget = NoMarkupStatic("", classes="question-option")
                self.option_widgets.append(widget)
                yield widget

            with Horizontal(classes="question-other-row"):
                self.other_prefix = NoMarkupStatic("", classes="question-other-prefix")
                yield self.other_prefix
                self.other_input = Input(
                    placeholder="Type your answer...", classes="question-other-input"
                )
                yield self.other_input
                self.other_static = NoMarkupStatic(
                    "Type your answer...", classes="question-other-static"
                )
                yield self.other_static

            self.submit_widget = NoMarkupStatic("", classes="question-submit")
            yield self.submit_widget

            self.help_widget = NoMarkupStatic("", classes="question-help")
            yield self.help_widget

    async def on_mount(self) -> None:
        self._question_renderer.update_display()
        self.focus()

    def _watch_current_question_idx(self) -> None:
        self._question_renderer.update_display()

    def _watch_selected_option(self) -> None:
        self._question_renderer.update_display()

    def action_move_up(self) -> None:
        self.selected_option = (self.selected_option - 1) % self._total_options

    def action_move_down(self) -> None:
        self.selected_option = (self.selected_option + 1) % self._total_options

    def action_select(self) -> None:
        if self._current_question.multi_select:
            self._selection_helper.handle_multi_select_action()
        else:
            self._selection_helper.handle_single_select_action()

    def action_cancel(self) -> None:
        self.post_message(self.Cancelled())

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        if not self.other_input or not self.other_input.value.strip():
            return

        q = self._current_question
        if q.multi_select:
            self.selected_option = len(q.options) + 1
        else:
            self._answer_manager.save_current_answer()
            self._selection_helper.advance_or_submit()

    def on_input_changed(self, _event: Input.Changed) -> None:
        self._answer_manager.store_other_text()
        self._selection_helper.sync_other_selection_with_text()
        self._question_renderer.update_display()

    def on_key(self, event: events.Key) -> None:
        if len(self.questions) <= 1:
            return
        if self.other_input and self.other_input.has_focus:
            return
        if event.key == "left":
            self._selection_helper.navigate_to_prev_question()
            event.stop()
        elif event.key == "right":
            self._selection_helper.navigate_to_next_question()
            event.stop()

    def _submit_answers(self) -> None:
        result: list[Answer] = []
        for i, q in enumerate(self.questions):
            answer_text, is_other = self.answers.get(i, ("", False))
            result.append(
                Answer(question=q.question, answer=answer_text, is_other=is_other)
            )
        self.post_message(self.Answered(answers=result))

    def on_blur(self, _event: events.Blur) -> None:
        self.call_after_refresh(self._ensure_focus)

    def on_input_blurred(self, _event: Input.Blurred) -> None:
        self.call_after_refresh(self._ensure_focus)

    def _ensure_focus(self) -> None:
        if self.has_focus or (self.other_input and self.other_input.has_focus):
            return
        self.focus()

import json
import logging
import os
import re
from pathlib import Path
from typing import Dict, Text, List, Any, Union, Tuple

import rasa.data
from rasa.nlu.training_data import Message
import rasa.utils.io as io_utils
from rasa.constants import (
    DEFAULT_E2E_TESTS_PATH,
    DOCS_URL_DOMAINS,
    DOCS_URL_STORIES,
    LEGACY_DOCS_BASE_URL,
)
from rasa.core.constants import INTENT_MESSAGE_PREFIX
from rasa.core.events import UserUttered
from rasa.core.exceptions import StoryParseError
from rasa.core.interpreter import RegexInterpreter
from rasa.core.training.story_reader.story_reader import StoryReader
from rasa.core.training.structures import StoryStep, FORM_PREFIX
from rasa.nlu.constants import INTENT_NAME_KEY, TEXT
import rasa.shared.utils.io

logger = logging.getLogger(__name__)


class MarkdownStoryReader(StoryReader):
    """Class that reads the core training data in a Markdown format

    """

    async def read_from_file(self, filename: Union[Text, Path]) -> List[StoryStep]:
        """Given a md file reads the contained stories."""

        try:
            with open(filename, "r", encoding=io_utils.DEFAULT_ENCODING) as f:
                lines = f.readlines()

            return await self._process_lines(lines)
        except ValueError as err:
            file_info = "Invalid story file format. Failed to parse '{}'".format(
                os.path.abspath(filename)
            )
            logger.exception(file_info)
            if not err.args:
                err.args = ("",)
            err.args = err.args + (file_info,)
            raise

    async def _process_lines(self, lines: List[Text]) -> List[StoryStep]:
        multiline_comment = False

        for idx, line in enumerate(lines):
            line_num = idx + 1
            try:
                line = self._replace_template_variables(self._clean_up_line(line))
                if line.strip() == "":
                    continue
                elif line.startswith("<!--"):
                    multiline_comment = True
                    continue
                elif multiline_comment and line.endswith("-->"):
                    multiline_comment = False
                    continue
                elif multiline_comment:
                    continue
                elif line.startswith(">>"):
                    # reached a new rule block
                    rule_name = line.lstrip(">> ")
                    self._new_rule_part(rule_name, self.source_name)
                elif line.startswith("#"):
                    # reached a new story block
                    name = line[1:].strip("# ")
                    self._new_story_part(name, self.source_name)
                elif line.startswith(">"):
                    # reached a checkpoint
                    name, conditions = self._parse_event_line(line[1:].strip())
                    self._add_checkpoint(name, conditions)
                elif re.match(fr"^[*\-]\s+{FORM_PREFIX}", line):
                    logger.debug(
                        "Skipping line {}, "
                        "because it was generated by "
                        "form action".format(line)
                    )
                elif line.startswith("-"):
                    # reached a slot, event, or executed action
                    event_name, parameters = self._parse_event_line(line[1:])
                    self._add_event(event_name, parameters)
                elif line.startswith("*"):
                    # reached a user message
                    user_messages = [el.strip() for el in line[1:].split(" OR ")]
                    if self.use_e2e:
                        await self._add_e2e_messages(user_messages, line_num)
                    else:
                        await self._add_user_messages(user_messages, line_num)
                else:
                    # reached an unknown type of line
                    logger.warning(
                        f"Skipping line {line_num}. "
                        "No valid command found. "
                        f"Line Content: '{line}'"
                    )
            except Exception as e:
                msg = f"Error in line {line_num}: {e}"
                logger.error(msg, exc_info=1)  # pytype: disable=wrong-arg-types
                raise ValueError(msg) from e
        self._add_current_stories_to_result()
        return self.story_steps

    @staticmethod
    def _parameters_from_json_string(s: Text, line: Text) -> Dict[Text, Any]:
        """Parse the passed string as json and create a parameter dict."""

        if s is None or not s.strip():
            # if there is no strings there are not going to be any parameters
            return {}

        try:
            parsed_slots = json.loads(s)
            if isinstance(parsed_slots, dict):
                return parsed_slots
            else:
                raise Exception(
                    "Parsed value isn't a json object "
                    "(instead parser found '{}')"
                    ".".format(type(parsed_slots))
                )
        except Exception as e:
            raise ValueError(
                "Invalid to parse arguments in line "
                "'{}'. Failed to decode parameters"
                "as a json object. Make sure the event"
                "name is followed by a proper json "
                "object. Error: {}".format(line, e)
            )

    def _replace_template_variables(self, line: Text) -> Text:
        def process_match(matchobject):
            varname = matchobject.group(1)
            if varname in self.template_variables:
                return self.template_variables[varname]
            else:
                raise ValueError(
                    "Unknown variable `{var}` "
                    "in template line '{line}'"
                    "".format(var=varname, line=line)
                )

        template_rx = re.compile(r"`([^`]+)`")
        return template_rx.sub(process_match, line)

    @staticmethod
    def _clean_up_line(line: Text) -> Text:
        """Removes comments and trailing spaces"""

        return re.sub(r"<!--.*?-->", "", line).strip()

    @staticmethod
    def _parse_event_line(line: Text) -> Tuple[Text, Dict[Text, Text]]:
        """Tries to parse a single line as an event with arguments."""

        # the regex matches "slot{"a": 1}"
        m = re.search("^([^{]+)([{].+)?", line)
        if m is not None:
            event_name = m.group(1).strip()
            slots_str = m.group(2)
            parameters = MarkdownStoryReader._parameters_from_json_string(
                slots_str, line
            )
            return event_name, parameters
        else:
            rasa.shared.utils.io.raise_warning(
                f"Failed to parse action line '{line}'. Ignoring this line.",
                docs=DOCS_URL_STORIES,
            )
            return "", {}

    async def _add_user_messages(self, messages: List[Text], line_num: int) -> None:
        if not self.current_step_builder:
            raise StoryParseError(
                "User message '{}' at invalid location. "
                "Expected story start.".format(messages)
            )
        parsed_messages = [self._parse_message(m, line_num) for m in messages]
        self.current_step_builder.add_user_messages(
            parsed_messages, self.unfold_or_utterances
        )

    async def _add_e2e_messages(self, e2e_messages: List[Text], line_num: int) -> None:
        if not self.current_step_builder:
            raise StoryParseError(
                "End-to-end message '{}' at invalid "
                "location. Expected story start."
                "".format(e2e_messages)
            )

        parsed_messages = []
        for m in e2e_messages:
            message = self.parse_e2e_message(m)
            parsed = self._parse_message(message.get(TEXT), line_num)
            parsed_messages.append(parsed)
        self.current_step_builder.add_user_messages(parsed_messages)

    @staticmethod
    def parse_e2e_message(line: Text) -> Message:
        """Parses an md list item line based on the current section type.

        Matches expressions of the form `<intent>:<example>`. For the
        syntax of `<example>` see the Rasa docs on NLU training data."""

        # Match three groups:
        # 1) Potential "form" annotation
        # 2) The correct intent
        # 3) Optional entities
        # 4) The message text
        form_group = fr"({FORM_PREFIX}\s*)*"
        item_regex = re.compile(r"\s*" + form_group + r"([^{}]+?)({.*})*:\s*(.*)")
        match = re.match(item_regex, line)

        if not match:
            raise ValueError(
                "Encountered invalid test story format for message "
                "`{}`. Please visit the documentation page on "
                "end-to-end testing at {}/user-guide/testing-your-assistant/"
                "#end-to-end-testing/".format(line, LEGACY_DOCS_BASE_URL)
            )
        from rasa.nlu.training_data import entities_parser

        intent = match.group(2)
        message = match.group(4)
        example = entities_parser.parse_training_example(message, intent)

        # If the message starts with the `INTENT_MESSAGE_PREFIX` potential entities
        # are annotated in the json format (e.g. `/greet{"name": "Rasa"})
        if message.startswith(INTENT_MESSAGE_PREFIX):
            parsed = RegexInterpreter().synchronous_parse(message)
            example.data["entities"] = parsed["entities"]

        return example

    def _parse_message(self, message: Text, line_num: int) -> UserUttered:
        parse_data = RegexInterpreter().synchronous_parse(message)

        text = None
        if self.use_e2e:
            text = parse_data.get("text")

        utterance = UserUttered(
            text, parse_data.get("intent"), parse_data.get("entities"), parse_data
        )

        intent_name = utterance.intent.get(INTENT_NAME_KEY)

        if self.domain and intent_name not in self.domain.intents:
            rasa.shared.utils.io.raise_warning(
                f"Found unknown intent '{intent_name}' on line {line_num}. "
                "Please, make sure that all intents are "
                "listed in your domain yaml.",
                UserWarning,
                docs=DOCS_URL_DOMAINS,
            )
        return utterance

    @staticmethod
    def is_markdown_story_file(file_path: Union[Text, Path]) -> bool:
        """Check if file contains Core training data or rule data in Markdown format.

        Args:
            file_path: Path of the file to check.

        Returns:
            `True` in case the file is a Core Markdown training data or rule data file,
            `False` otherwise.
        """
        if not rasa.data.is_likely_markdown_file(file_path) or rasa.data.is_nlu_file(
            file_path
        ):
            return False

        try:
            with open(
                file_path, encoding=io_utils.DEFAULT_ENCODING, errors="surrogateescape"
            ) as lines:
                return any(
                    MarkdownStoryReader._contains_story_or_rule_pattern(line)
                    for line in lines
                )
        except Exception as e:
            # catch-all because we might be loading files we are not expecting to load
            logger.error(
                f"Tried to check if '{file_path}' is a story file, but failed to "
                f"read it. If this file contains story or rule data, you should "
                f"investigate this error, otherwise it is probably best to "
                f"move the file to a different location. Error: {e}"
            )
            return False

    @staticmethod
    def is_markdown_test_stories_file(file_path: Union[Text, Path]) -> bool:
        """Checks if a file contains test stories.

        Args:
            file_path: Path of the file which should be checked.

        Returns:
            `True` if it's a file containing test stories, otherwise `False`.
        """
        if not rasa.data.is_likely_markdown_file(file_path):
            return False

        dirname = os.path.dirname(file_path)
        return (
            DEFAULT_E2E_TESTS_PATH in dirname
            and rasa.data.is_story_file(file_path)
            and not rasa.data.is_nlu_file(file_path)
        )

    @staticmethod
    def _contains_story_or_rule_pattern(text: Text) -> bool:
        story_pattern = r".*##.+"
        rule_pattern = r".*>>.+"

        return any(re.match(pattern, text) for pattern in [story_pattern, rule_pattern])

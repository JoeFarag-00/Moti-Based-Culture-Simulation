import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from langchain import LLMChain
from langchain.base_language import BaseLanguageModel
from agent.memory import GenerativeAgentMemory
from langchain.prompts import PromptTemplate

from transformers import BertTokenizer, BertModel
import torch

class GenerativeAgent(BaseModel):
    """A character with memory and innate characteristics."""

    name: str
    """The character's name."""

    age: Optional[int] = None
    """The optional age of the character."""
    traits: str = "N/A"
    """Permanent traits to ascribe to the character."""
    status: str
    """The traits of the character you wish not to change."""
    memory: GenerativeAgentMemory
    """The memory object that combines relevance, recency, and 'importance'."""
    llm: BaseLanguageModel
    """The underlying language model."""
    verbose: bool = False
    summary: str = ""  #: :meta private:
    """Stateful self-summary generated via reflection on the character's memory."""

    summary_refresh_seconds: int = 3600  #: :meta private:
    """How frequently to re-generate the summary."""

    last_refreshed: datetime = Field(default_factory=datetime.now)  # : :meta private:
    """The last time the character's summary was regenerated."""

    daily_summaries: List[str] = Field(default_factory=list)  # : :meta private:
    """Summary of the events in the plan that the agent took."""

    class Config:
        """Configuration for this pydantic object."""

        arbitrary_types_allowed = True

    @staticmethod
    def _parse_list(text: str) -> List[str]:
        """Parse a newline-separated string into a list of strings."""
        lines = re.split(r"\n", text.strip())
        return [re.sub(r"^\s*\d+\.\s*", "", line).strip() for line in lines]

    def chain(self, prompt: PromptTemplate) -> LLMChain:
        return LLMChain(
            llm=self.llm, prompt=prompt, verbose=self.verbose, memory=self.memory
        )

    def _get_entity_from_observation(self, observation: str) -> str:
        prompt = PromptTemplate.from_template(
            "What is the observed entity in the following observation? {observation}"
            + "\nEntity="
        )
        return self.chain(prompt).run(observation=observation).strip()

    def _get_entity_action(self, observation: str, entity_name: str) -> str:
        prompt = PromptTemplate.from_template(
            "What is the {entity} doing in the following observation? {observation}"
            + "\nThe {entity} is"
        )
        return (
            self.chain(prompt).run(entity=entity_name, observation=observation).strip()
        )

    def summarize_related_memories(self, observation: str) -> str:
        """Summarize memories that are most relevant to an observation."""
        prompt = PromptTemplate.from_template(
            """
{q1}?
Context from memory:
{relevant_memories}
Relevant context: 
"""
        )
        entity_name = self._get_entity_from_observation(observation)
        entity_action = self._get_entity_action(observation, entity_name)
        q1 = f"What is the relationship between {self.name} and {entity_name}"
        q2 = f"{entity_name} is {entity_action}"
        return self.chain(prompt=prompt).run(q1=q1, queries=[q1, q2]).strip()

    def _generate_reaction(
        self, observation: str, areas: str, suffix: str, now: Optional[datetime] = None
    ) -> str:
        """React to a given observation or dialogue act."""
        prompt = PromptTemplate.from_template(
            "{agent_summary_description}"
            + "\nIt is {current_time}."
            + "\n{agent_name}'s status: {agent_status}"
            + "\nSummary of relevant context from {agent_name}'s memory:"
            + "\n{relevant_memories}"
            + "\nMost recent observations: {most_recent_memories}"
            + "\nObservation: {observation}"
            + "\n\n"
            + suffix
        )
        agent_summary_description = self.get_summary(now=now)
        relevant_memories_str = self.summarize_related_memories(observation)
        current_time_str = (
            datetime.now().strftime("%B %d, %Y, %I:%M %p")
            if now is None
            else now.strftime("%B %d, %Y, %I:%M %p")
        )
        kwargs: Dict[str, Any] = dict(
            agent_summary_description=agent_summary_description,
            current_time=current_time_str,
            relevant_memories=relevant_memories_str,
            agent_name=self.name,
            areas=areas,
            observation=observation,
            agent_status=self.status,
        )

        consumed_tokens = self.llm.get_num_tokens(
            prompt.format(most_recent_memories="", **kwargs)
        )
        # print("Token: ",consumed_tokens)
        # print(prompt.format(most_recent_memories="", **kwargs))
        kwargs[self.memory.most_recent_memories_token_key] = consumed_tokens
        return self.chain(prompt=prompt).run(**kwargs).strip()

    def _clean_response(self, text: str) -> str:
        return re.sub(f"^{self.name} ", "", text.strip()).strip()

    def generate_reaction(
        self, observation: str, areaList: list, now: Optional[datetime] = None
    ) -> Tuple[bool, str]:
        """React to a given observation."""
        call_to_action_template = (
            "Should {agent_name} react to the observation, and if so,"
            + " what would be an appropriate reaction? Respond in one line."
            + ' If the action is to engage in dialogue, write:\nSAY: "what to say"'
            + "\notherwise, write:\nREACT: {agent_name}'s reaction (if anything)."
            + "\notherwise, write:\nEXPLORE: where do you want to explore."
            + "\nEither do nothing, react, or say something but not both.\n"
            + "\nFinally, choose a area to do your reaction from Area List. Wrap area with <>."
            + "\nArea List: ```{areas}```\n\n"
        )

        full_result = self._generate_reaction(
            observation, areaList, call_to_action_template, now=now
        )
        result = full_result.strip().split("\n")[0]
        # AAA
        self.memory.save_context(
            {},
            {
                self.memory.add_memory_key: f"{self.name} observed "
                f"{observation} and reacted by {result}",
                self.memory.now_key: now,
            },
        )
        if "REACT:" in result:
            reaction = self._clean_response(result.split("REACT:")[-1])
            try:
                area = re.search(r"<(.+?)>", result).group(1)
                return False, f"{self.name} {reaction}", area
            except Exception as e:
                return False, f"{self.name} {reaction}", None
        if "SAY:" in result:
            said_value = self._clean_response(result.split("SAY:")[-1])
            try:
                area = re.search(r"<(.+?)>", result).group(1)
                return True, f"{self.name} said {said_value}", area
            except Exception as e:
                return False, f"{self.name} said {said_value}", None
            
        if "EXPLORE" in result:
            reaction = self._clean_response(result.split("EXPLORE:")[-1])
            try:
                area = re.search(r"<(.+?)>", result).group(1)
                return False, f"{self.name} {reaction}", area
            except Exception as e:
                return False, f"{self.name} {reaction}", None
        else:
            return False, result, None

    def generate_dialogue_response(
        self, observation: str, now: Optional[datetime] = None
    ) -> Tuple[bool, str]:
        """React to a given observation."""
        call_to_action_template = (
            "What would {agent_name} say? To end the conversation, write:"
            ' GOODBYE: "what to say". Otherwise to continue the conversation,'
            ' write: SAY: "what to say next"\n\n'
        )
        full_result = self._generate_reaction(
            observation, call_to_action_template, now=now
        )
        result = full_result.strip().split("\n")[0]
        if "GOODBYE:" in result:
            farewell = self._clean_response(result.split("GOODBYE:")[-1])
            self.memory.save_context(
                {},
                {
                    self.memory.add_memory_key: f"{self.name} observed "
                    f"{observation} and said {farewell}",
                    self.memory.now_key: now,
                },
            )
            return False, f"{self.name} said {farewell}"
        if "SAY:" in result:
            response_text = self._clean_response(result.split("SAY:")[-1])
            self.memory.save_context(
                {},
                {
                    self.memory.add_memory_key: f"{self.name} observed "
                    f"{observation} and said {response_text}",
                    self.memory.now_key: now,
                },
            )
            return True, f"{self.name} said {response_text}"
        else:
            return False, result

    ######################################################
    # Agent stateful' summary methods.                   #
    # Each dialog or response prompt includes a header   #
    # summarizing the agent's self-description. This is  #
    # updated periodically through probing its memories  #
    ######################################################
    def _compute_agent_summary(self) -> str:
        """"""
        prompt = PromptTemplate.from_template(
            "How would you summarize {name}'s core characteristics given the"
            + " following statements:\n"
            + "{relevant_memories}"
            + "Do not embellish."
            + "\n\nSummary: "
        )
        # The agent seeks to think about their core characteristics.
        return (
            self.chain(prompt)
            .run(name=self.name, queries=[f"{self.name}'s core characteristics"])
            .strip()
        )

    def _compute_agent_BERT(self) -> str:
        
        tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
        model = BertModel.from_pretrained('bert-base-uncased')
    
        input_memories = "{relevant_memories}"
        input_ids = tokenizer.encode(input_memories, add_special_tokens=True, return_tensors='pt')

        with torch.no_grad():
            outputs = model(input_ids)
            # summary = outputs[0]  

        decoded_summary = tokenizer.decode(input_ids[0], skip_special_tokens=True)
        summary_sentences = decoded_summary.split(".")[:2]  
        Summerized_Reflection = ". ".join(summary_sentences)

        return Summerized_Reflection
        
        
    def get_summary(
        self, force_refresh: bool = False, now: Optional[datetime] = None
    ) -> str:
        """Return a descriptive summary of the agent."""
        current_time = datetime.now() if now is None else now
        since_refresh = (current_time - self.last_refreshed).seconds
        if (
            not self.summary
            or since_refresh >= self.summary_refresh_seconds
            or force_refresh
        ):
            self.summary = self._compute_agent_summary()
            self.last_refreshed = current_time
        age = self.age if self.age is not None else "N/A"
        return (
            f"Name: {self.name} (age: {age})"
            + f"\nInnate traits: {self.traits}"
            + f"\n{self.summary}"
        )

    def get_full_header(
        self, force_refresh: bool = False, now: Optional[datetime] = None
    ) -> str:
        """Return a full header of the agent's status, summary, and current time."""
        now = datetime.now() if now is None else now
        summary = self.get_summary(force_refresh=force_refresh, now=now)
        current_time_str = now.strftime("%B %d, %Y, %I:%M %p")
        return (
            f"{summary}\nIt is {current_time_str}.\n{self.name}'s status: {self.status}"
        )

import json
import math
import string
import sys
import warnings
from abc import ABC, abstractmethod
from copy import deepcopy
from typing import Generic, Optional, Type, TypeVar, Union
import warnings
import re

import numpy as np
import requests
from langchain import PromptTemplate
from langchain.chains import LLMChain
from transformers import pipeline
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler

from openelm.configs import (
    EnvConfig,
    ImageEnvConfig,
    P3ProblemEnvConfig,
    P3ProbSolEnvConfig,
    PromptEnvConfig,
    SodaraceEnvConfig,
    StringEnvConfig,
)
from openelm.environments.env_utils import NULL_SEED, ToyPromptTask, get_image_target
from openelm.environments.sodaracer import (
    CIRCLE,
    GALLOPER_PREREQ,
    IMPORTS,
    INSTRUCTIONS,
    QUERY_CPPN,
    SEEDS_DICT,
    SQUARE_PREREQ,
    SodaraceSimulator,
    Walker,
)
from openelm.environments.p3 import (
    P3_PROBLEM_MED_SEED,
    P3_PROBLEM_LONG_SEED,
    P3_PROBSOL_LONG_SEED,
    P3_IMPORTS,
)
from openelm.mutation_model import MutationModel
from openelm.utils.code_eval import pool_exec_processes, type_check
from openelm.sandbox.server.sandbox_codex_execute import ExecResult

sys.set_int_max_str_digits(0)  # remove length limitation for int->str conversion
# (model sometimes outputs really long ints)

Phenotype = Optional[np.ndarray]


def ackley(x: np.ndarray) -> np.ndarray:
    d = x.shape[-1]
    a = 5
    b = 0.1

    o1 = -a * np.exp(-b * np.sqrt(np.sum(x**2, axis=1) / d))
    o2 = -np.exp(np.sum(np.cos(math.tau * x) / d, axis=1))

    return -(a + math.exp(1) + o1 + o2)


def numpy_to_ascii_art(arr):
    """Convert a numpy array with dimensions (width, height, channels) to ascii art."""
    art_chars = " .:-=#"
    im = np.sum(arr, axis=-1)  # we can't do colors
    idx = np.round(np.interp(im, (im.min(), im.max()), (0, len(art_chars) - 1))).astype(
        "int"
    )
    chars = np.choose(idx, art_chars)
    ascii_art = "\n".join(["".join(x) for x in chars])
    return ascii_art


class Genotype(ABC):
    def __str__(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def to_phenotype(self) -> Optional[Phenotype]:
        raise NotImplementedError


GenoType = TypeVar("GenoType", bound=Genotype)


class BaseEnvironment(ABC, Generic[GenoType]):
    def __init__(self) -> None:
        self.genotype_space: np.ndarray
        self.batch_size: int
        self.config: EnvConfig

    @abstractmethod
    def get_rng_state(self) -> Optional[np.random._generator.Generator]:
        raise NotImplementedError

    @abstractmethod
    def set_rng_state(self, rng_state: Optional[np.random._generator.Generator]):
        raise NotImplementedError

    @abstractmethod
    def random(self) -> list[GenoType]:
        raise NotImplementedError

    @abstractmethod
    def mutate(self, x: list[GenoType]) -> list[GenoType]:
        raise NotImplementedError

    @abstractmethod
    def fitness(self, x: GenoType) -> float:
        raise NotImplementedError

    @property
    def max_fitness(self) -> int:
        return 0

    @property
    # [starts, endings) of search intervals
    def behavior_space(self) -> np.ndarray:
        return self.genotype_space

    @property
    def behavior_ndim(self) -> int:
        return self.behavior_space.shape[1]


class ArrayGenotype(Genotype, np.ndarray):
    def __new__(cls, input_array):
        obj = np.asarray(input_array).view(cls)
        return obj

    def __str__(self) -> str:
        return f'({", ".join(map(str, np.asarray(self)))})'

    def to_phenotype(self) -> Phenotype:
        return np.asarray(self)


# find all local maxima of a multimodal function
class FunctionOptim(BaseEnvironment[ArrayGenotype]):
    def __init__(self, ndim=2, seed=None):
        self.genotype_ndim = ndim
        self.genotype_space = np.repeat([[-4, 4]], self.genotype_ndim, axis=0).T
        self.batch_size: int = 1
        self.rng = np.random.default_rng(seed)

    def get_rng_state(self) -> Optional[np.random._generator.Generator]:
        return self.rng

    def set_rng_state(self, rng_state: Optional[np.random._generator.Generator]):
        self.rng = rng_state

    def random(self) -> list[ArrayGenotype]:
        return [
            ArrayGenotype(self.rng.uniform(*self.genotype_space))
            for _ in range(self.batch_size)
        ]

    def mutate(self, x: list[ArrayGenotype]) -> list[ArrayGenotype]:
        for i in range(self.batch_size):
            ix = self.rng.integers(self.genotype_ndim)
            x[i][ix] = x[i][ix] + self.rng.uniform(-1, 1)
        return x

    def fitness(self, x: ArrayGenotype) -> float:
        return ackley(x[None])[0]


class StringArrayGenotype(ArrayGenotype):
    def __str__(self) -> str:
        x: np.ndarray = np.round(self)
        return "".join(
            string.ascii_letters[ix]
            for ix in np.clip(x.astype(int), 0, len(string.ascii_letters) - 1)
        )

    def to_phenotype(self) -> Phenotype:
        return np.asarray(self)


class MatchString(BaseEnvironment[StringArrayGenotype]):
    # find a string by mutating one character at a time

    def __init__(self, config: StringEnvConfig):
        self.alphabet = string.ascii_letters

        self.config: StringEnvConfig = config
        self.batch_size = self.config.batch_size
        self.target = np.array([self.alphabet.index(ch) for ch in self.config.target])
        self.genotype_ndim = self.target.shape[0]
        self.genotype_space = np.repeat(
            [[0, len(self.alphabet)]], self.genotype_ndim, axis=0
        ).T
        self.rng = np.random.default_rng(self.config.seed)

    def get_rng_state(self) -> Optional[np.random._generator.Generator]:
        return self.rng

    def set_rng_state(self, rng_state: Optional[np.random._generator.Generator]):
        self.rng = rng_state

    def random(self) -> list[StringArrayGenotype]:
        return [
            StringArrayGenotype(self.rng.uniform(*self.genotype_space))
            for _ in range(self.batch_size)
        ]

    def mutate(self, genomes: list[StringArrayGenotype]) -> list[StringArrayGenotype]:
        x = deepcopy(genomes)
        for i in range(self.batch_size):
            ix = self.rng.integers(self.genotype_ndim)
            x[i][ix] = x[i][ix] + self.rng.uniform(-1, 1)
        return x

    def fitness(self, x: StringArrayGenotype) -> float:
        return -np.abs(x.to_phenotype() - self.target).sum()


class PromptGenotype(Genotype):
    """
    Genotype wrapper for a LangChain template.

    This consists of a base format for all individuals, as well as individual-specific fields which will be evolved.
    Remaining fields will be filled in at evaluation time.

    Args:
        prompt (PromptTemplate): The base template for all individuals.
        fixed_inputs (dict[str, str], optional): Individual-specific fields to fill in. Defaults to None.
    """

    def __init__(
        self, prompt: PromptTemplate, fixed_inputs: Optional[dict[str, str]] = None
    ):
        self.fixed_inputs = fixed_inputs
        if fixed_inputs:
            self.prompt = prompt.partial(**fixed_inputs)
        else:
            self.prompt = prompt
        self.result_obj = None

    def __str__(self) -> str:
        return self.prompt.template

    def format(self, **kwargs) -> str:
        return self.prompt.format(**kwargs)

    def evaluate(self, model, inputs):
        chain = LLMChain(llm=model.model, prompt=self.prompt)
        self.result_obj = {
            "prompt": self.format(**inputs),
            "output": chain(inputs),
        }
        return self.result_obj["output"]

    def to_phenotype(self) -> str:
        return (0.0,)


class PromptEvolution(BaseEnvironment[PromptGenotype]):
    """Evolves a LangChain prompt."""

    def __init__(
        self,
        config: PromptEnvConfig,
        mutation_model: MutationModel,
        fitness_model=None,
    ):
        self.config: PromptEnvConfig = config
        self.batch_size = self.config.batch_size
        self.genotype_ndim = 1
        self.genotype_space = np.array([[0], [1]])
        self.mutation_model = mutation_model
        if fitness_model is None:
            self.fitness_model = mutation_model
        self.task = ToyPromptTask()
        self.base_prompt = PromptTemplate(
            template=self.task.base_template, input_variables=self.task.input_variables
        )
        self.rng = np.random.default_rng(self.config.seed)

    def get_rng_state(self) -> Optional[np.random._generator.Generator]:
        return self.rng

    def set_rng_state(self, rng_state: Optional[np.random._generator.Generator]):
        self.rng = rng_state

    def random(self) -> list[PromptGenotype]:
        return [self.random_prompt() for _ in range(self.batch_size)]

    def random_prompt(self):
        inputs = {
            "n_repetitions": str(self.rng.integers(10)),
            "instruction_str": self.task.instruction_str,
            "few_shot_examples": self.task.create_few_shot_examples(
                self.task.instruction_str
            ),
        }
        return PromptGenotype(prompt=self.base_prompt, fixed_inputs=inputs)

    def mutate(self, genomes: list[PromptGenotype]) -> list[PromptGenotype]:
        prompts = [self.mutate_prompt(prompt) for prompt in genomes]
        return prompts

    def mutate_prompt(self, prompt):
        # mutate the instruction string; note that we also need to change the few shot examples to match
        old_instruction_str = prompt.fixed_inputs["instruction_str"]
        mutation_prompt = PromptTemplate(
            input_variables=["instruction_str"],
            template=self.task.mutation_instruction,
        )
        mutation_chain = LLMChain(llm=self.fitness_model.model, prompt=mutation_prompt)
        result = mutation_chain({"instruction_str": old_instruction_str})
        new_instruction_str = result["text"].strip().split()[0]

        inputs = {
            "n_repetitions": str(np.random.randint(10)),
            "instruction_str": new_instruction_str,
            "few_shot_examples": self.task.create_few_shot_examples(
                new_instruction_str
            ),
        }

        return PromptGenotype(prompt=self.base_prompt, fixed_inputs=inputs)

    def fitness(self, x: PromptGenotype) -> float:
        inputs = {
            "target": self.task.target,
        }
        result = x.evaluate(model=self.fitness_model, inputs=inputs)

        # fitness is number of times it generated the target word in a row
        count = 0
        for word in result["text"].strip().split():
            if word.lower() == self.task.target:
                count += 1
            else:
                break

        fitness = count
        if self.config.debug:
            print(f"-- Prompt --\n{x.result_obj['prompt']}\n-- Fitness: {fitness} --")

        return fitness


class ImageGeneration(Genotype):
    """Genotype for generated images."""

    def __init__(self, program_str: str, result_obj: np.ndarray):
        self.program_str = program_str
        self.result_obj = result_obj
        self.valid = self.validate()

    def __str__(self) -> str:
        if self.valid:
            return numpy_to_ascii_art(self.result_obj)
            # return str(self.result_obj.reshape((-1, 3)).mean(axis=0).astype(int))
        else:
            return ""

    def validate(self) -> bool:
        return len(self.result_obj.shape) == 3 and self.result_obj.shape[2] == 3

    def to_phenotype(self, mode: str = "3-channel-avg") -> Optional[Phenotype]:
        if not self.valid:
            return None
        if mode == "3-channel-avg":
            # Average RGB channels.
            # Assume the input is of shape (height, width, channel), and we
            # average each channel to get (channel,)
            return np.average(self.result_obj.reshape((-1, 3)), axis=0)
        else:
            return None

    def visualize(self, ax) -> None:
        if self.valid:
            ax.imshow(self.result_obj)


class ImageOptim(BaseEnvironment[ImageGeneration]):
    """
    Mutate programs that return images.

    Fitness is simply the absolute difference between the returning
    image and the target image. To map into the behavior space,
    if behavior_ndims=="3-channel", the image will be divided into blocks
    (specified in `block_size`), and average
    values of RGB channels in each block will be put together as a point in the
    behavior space (average-pooling).
    """

    # Record different definitions of behavior spaces in a dict.
    behavior_ndims = {"3-channel": 3}

    def __init__(
        self,
        config: ImageEnvConfig,
        mutation_model: MutationModel,
    ):
        self.config: ImageEnvConfig = config
        self.batch_size = self.config.batch_size
        self.target_img: np.ndarray = get_image_target(self.config.target)
        self.seed: str = NULL_SEED
        self.mutation_model: MutationModel = mutation_model

        self.behavior_mode: str = self.config.behavior_mode
        self.genotype_ndim: int = self.behavior_ndims[self.behavior_mode]
        self.genotype_space = np.repeat([[0, 255]], self.genotype_ndim, axis=0).T
        self.rng = None

    def get_rng_state(self) -> Optional[np.random._generator.Generator]:
        warnings.warn("WARNING: rng state not used in this environment")
        return None

    def set_rng_state(self, rng_state: Optional[np.random._generator.Generator]):
        warnings.warn("WARNING: rng state not used in this environment")
        pass

    def construct_prompt(
        self, code_batch: Optional[Union[list[str], str]] = None
    ) -> dict[str, str]:
        prompt_str: str = """
import math
import numpy as np
"""
        instruction_str: str = """
# Fixed version of draw()
def draw():
    \"\"\"
    Draws a yellow circle with radius 10 in the middle of a 32 by 32 black image.

    Returns:
        np.ndarray: the image
    \"\"\"
    pic = np.zeros((32, 32, 3))
"""
        import_str: str = prompt_str
        if code_batch is None:
            # Initialization steps
            prompt_str += self.seed
        else:
            prompt_str += """
# Old version of draw()
# TODO: fix bugs in the code below

"""
            # Evolution steps
            if isinstance(code_batch, list):
                prompt_str += code_batch[0]
            elif isinstance(code_batch, str):
                prompt_str += code_batch
        import_str += instruction_str
        prompt_str += instruction_str
        return {"prompt": prompt_str, "template": import_str}

    def generate_programs(
        self, code_batch: list[dict[str, str]]
    ) -> list[ImageGeneration]:
        func_name: str = "draw"
        generated_programs = self.mutation_model.generate_programs(
            code_batch, local_scope_truncate=True
        )
        if self.config.sandbox:
            results = []
            for code in generated_programs:
                resp = requests.post(
                    f"{self.config.sandbox_server}/eval_imageoptim_func",
                    json={
                        "code": code,
                        "func_name": func_name,
                        "timeout": self.config.timeout,
                    },
                    timeout=self.config.timeout,
                )
                if resp.status_code == 200:
                    return_dict = json.loads(resp.text)
                    results.append(return_dict)
            return [ImageGeneration(**p) for p in results]
        # for i in range(len(results)):
        #     results[i]["result_obj"] = np.array(results[i]["result_obj"])
        # return results
        else:
            results = pool_exec_processes(
                generated_programs,
                func_name=func_name,
                timeout=self.config.timeout,
                processes=self.config.processes,
                debug=self.config.debug,
            )
            result_list: list = []
            for i, result in enumerate(results):
                try:
                    if isinstance(result, np.ndarray):
                        result_list.append(
                            {
                                "program_str": generated_programs[i],
                                "result_obj": result,
                            }
                        )
                    else:
                        if self.config.debug:
                            print("Failed execution, type:", result)
                            print(generated_programs[i])
                except Exception as e:
                    if self.config.debug:
                        print(type(e), e)
            return [ImageGeneration(**p) for p in result_list]

    def random(self) -> list[ImageGeneration]:
        program_list = [self.construct_prompt() for _ in range(self.config.batch_size)]
        new_images = self.generate_programs(program_list)
        return new_images

    def mutate(self, images_list: list[ImageGeneration]) -> list[ImageGeneration]:
        images = [img.program_str for img in images_list]
        program_list = list(map(self.construct_prompt, images))
        new_images = self.generate_programs(program_list)
        return new_images

    def fitness(self, x: ImageGeneration) -> float:
        if not x.valid or x.result_obj.shape != self.target_img.shape:
            return -np.inf
        return -np.abs(x.result_obj - self.target_img).sum()


class Sodaracer(Genotype):
    def __init__(self, program_str: str, result_obj: dict):
        """
        The Sodaracer genotype.

        Args:
            program_str: the string for the original code.
            result_obj: the dict of sodaracer.
        """
        self.program_str: str = program_str
        self.result_obj: dict = result_obj

        # Check whether the Sodaracer is valid.
        try:
            # Test the Sodaracer by instantiating a simulation.
            self.simulator = SodaraceSimulator(body=self.result_obj)
            self.morphology = self.simulator.morphology
            self.evaluate(0)
            self.valid = True
        except Exception:
            self.valid = False

    def evaluate(self, eval_ms: int) -> float:
        self._fitness = self.simulator.evaluate(eval_ms)
        # if self._fitness is None:
        #     print(self.valid)
        #     self.simulator = SodaraceSimulator(body=self.result_obj)
        #     print(self.evaluate(0))
        return self._fitness

    def __str__(self) -> str:
        return self.program_str

    def to_phenotype(self) -> Optional[Phenotype]:
        if self.valid:
            return np.array(
                [
                    self.morphology["height"],
                    self.morphology["width"],
                    self.morphology["mass"],
                ]
            ).astype(int)
        else:
            return None

    @property
    def fitness(self) -> Optional[float]:
        return self._fitness


class Sodarace(BaseEnvironment[Sodaracer]):
    def __init__(
        self,
        config: SodaraceEnvConfig,
        mutation_model: MutationModel,
    ) -> None:
        """
        Sodarace environment.

        Args:
            config: the environment config.
            mutation_model: the mutation model.
        """
        self.config: SodaraceEnvConfig = config
        self.batch_size = self.config.batch_size
        self.mutation_model: MutationModel = mutation_model

        self.genotype_space = np.array(self.config.behavior_space).T
        self.genotype_ndim = self.genotype_space.shape[1]

        self.seed_strs: list[str] = self.config.starting_seeds
        self.rng = None

    def get_rng_state(self) -> Optional[np.random._generator.Generator]:
        warnings.warn("WARNING: rng state not used in this environment")
        return None

    def set_rng_state(self, rng_state: Optional[np.random._generator.Generator]):
        warnings.warn("WARNING: rng state not used in this environment")
        pass

    def construct_prompt(
        self, code_batch: Optional[Union[list[str], str]] = None
    ) -> dict[str, str]:
        """
        Constructs a prompt for generating Sodaracers.

        Args:
            code_batch (Optional[Union[list[str], str]], optional): A
            list of program strings or a single program string. Defaults to None.

        Returns:
            dict[str, str]: A dictionary containing two keys: "prompt" and
            "template". The "prompt" key maps to a string containing the
            full prompt for generating a Sodaracer program. The "template"
            key maps to a string containing the required imports and
            instruction for generating a Sodaracer program.

        The method constructs a prompt for generating Sodaracer programs
        based on the seeds and configuration settings specified in self.seed_strs
        and self.config.
        """
        prompt_str: str = IMPORTS
        if "square" in self.seed_strs:
            prompt_str += SQUARE_PREREQ
        if "galloper" in self.seed_strs:
            prompt_str += GALLOPER_PREREQ
        if "radial" in self.seed_strs or "wheel" in self.seed_strs:
            prompt_str += CIRCLE
        if (
            "cppn_fixed" in self.seed_strs
            or "cppn_mutable" in self.seed_strs
            or "runner" in self.seed_strs
        ):
            prompt_str += QUERY_CPPN
        # For crossover:
        # If init steps, combine seeds and prereqs, and use instruction 3 code below.
        # For all other steps, prepend all prereqs and ignore instruction 3 code.
        # For non-crossover
        # Always preprend prereq, and len(code_batch) == 1
        import_str: str = prompt_str
        if code_batch is None:
            # Initialization steps
            seeds = [SEEDS_DICT[seed] for seed in self.seed_strs]
            if not self.config.crossover:
                # TODO: Sample from seeds randomly
                prompt_str += seeds[0]
            elif self.config.crossover:
                if self.config.instruction == 3:
                    instruction_str: str = INSTRUCTIONS[self.config.instruction].split(
                        ","
                    )[0]
                for seed in seeds:
                    prompt_str += seed
                    if self.config.instruction == 3:
                        reverse_seeds: dict[str, str] = {
                            v: k for k, v in SEEDS_DICT.items()
                        }
                        instruction_str += reverse_seeds[seed] + ", "
                if self.config.instruction == 3:
                    instruction_str += INSTRUCTIONS[self.config.instruction].split(",")[
                        1
                    ]
                raise NotImplementedError
        else:
            # Evolution steps
            if not self.config.crossover:
                if isinstance(code_batch, list):
                    # TODO: get nearby genotypes
                    prompt_str += code_batch[0]
                elif isinstance(code_batch, str):
                    prompt_str += code_batch
            elif self.config.crossover:
                # Crossover
                raise NotImplementedError
        instruction_str = INSTRUCTIONS[self.config.instruction]
        import_str += instruction_str
        prompt_str += instruction_str
        return {"prompt": prompt_str, "template": import_str}

    def generate_programs(self, code_batch: list[dict[str, str]]) -> list[Sodaracer]:
        """
        Generate new programs with a mutation model and evaluate them.

        Args:
            code_batch (list[dict[str, str]): a list of program strings.

        Returns:
            list[Sodaracer]: A list of Sodaracer objects.
        """
        local_scope_exec: bool = self.config.instruction != 0
        generated_programs = self.mutation_model.generate_programs(
            code_batch, local_scope_exec
        )
        if self.config.sandbox:
            results = []
            for code in generated_programs:
                resp = requests.post(
                    f"{self.config.sandbox_server}/gen_racer",
                    json={"code": code, "timeout": self.config.timeout},
                    timeout=self.config.timeout,
                )
                if resp.status_code == 200:
                    return_dict = json.loads(resp.text)
                    results.append(return_dict)
            return [Sodaracer(**p) for p in results]
        else:
            results = pool_exec_processes(
                generated_programs,
                func_name="make_walker",
                timeout=self.config.timeout,
                processes=self.config.processes,
                debug=self.config.debug,
            )
            result_list: list = []
            for i, result in enumerate(results):
                try:
                    if isinstance(result, Walker) and result.validate():
                        result_list.append(
                            {
                                "program_str": generated_programs[i],
                                "result_obj": result.to_dict(),
                            }
                        )
                    else:
                        if self.config.debug:
                            print("Failed execution, type:", result)
                            print(generated_programs[i])
                except Exception as e:
                    if self.config.debug:
                        print(type(e), e)
            return [Sodaracer(**p) for p in result_list]

    def random(self) -> list[Sodaracer]:
        """
        Generates a batch of Sodaracer programs with the specified batch size.

        Returns a list of new Sodaracer programs.

        Returns:
            list[Sodaracer]: A list of random Sodaracer programs.
        """
        program_list = [self.construct_prompt() for _ in range(self.config.batch_size)]
        new_sodaracers = self.generate_programs(program_list)
        return new_sodaracers

    def mutate(self, sodaracer_list: list[Sodaracer]) -> list[Sodaracer]:
        """
        Mutates a list of Sodaracer programs.

        Given a list of Sodaracer programs, constructs a prompt for each program,
        generate a list of new programs by mutating the prompts, and returns a
        list of new Sodaracer programs.

        Args:
            sodaracer_list (list[Sodaracer]): A list of Sodaracer programs to be mutated.

        Returns:
            list[Sodaracer]: A list of new Sodaracer programs generated by mutating the prompts.
        """
        sodaracers = [sr.program_str for sr in sodaracer_list]
        program_list = list(map(self.construct_prompt, sodaracers))
        new_sodaracers = self.generate_programs(program_list)
        return new_sodaracers

    def fitness(self, x: Sodaracer) -> float:
        """
        Evaluates the fitness of a Sodaracer program.

        Args:
            x (Sodaracer): A Sodaracer to evaluate.

        Returns:
            float: fitness of the Sodaracer.

        The method first checks whether the Sodaracer program is valid or not using
        the `.evaluate()` method of the Sodaracer. If the program is invalid,
        the method returns -np.inf to indicate that the program is not fit.
        """
        if x.valid:
            return x.evaluate(self.config.eval_ms)
        else:
            return -np.inf


class P3Solution(Genotype):
    def __init__(self, program_str: str, result_obj: dict, model: str):
        """
        Genotype for a programming puzzle solution.
        Args:
            program_str: the solution program string (the g6() function).
            result_obj: dict.
        """
        self.program_str = program_str
        self.result_obj = result_obj
        self.model = model

        self.pl = pipeline('feature-extraction', model=self.model)
        seed_features = np.array(self.pl(P3_PROBLEM_LONG_SEED))
        
        self.scaler = StandardScaler()
        seed_features_scaled = self.scaler.fit_transform(np.squeeze(seed_features))
        self.pca = PCA(.95)
        self.pca.fit(seed_features_scaled)

    def __str__(self) -> str:
        return self.program_str

    def to_phenotype(self) -> Optional[Phenotype]:
        features = np.array(self.pl(self.program_str))
        features_scaled = self.scaler.transform(np.squeeze(features))
        pca_features = self.pca.transform(features_scaled)
        return pca_features.max(axis=0).flatten()


class P3Problem(BaseEnvironment[P3Solution]):

    def __init__(
        self,
        config: P3ProblemEnvConfig,
        mutation_model: MutationModel,
    ) -> None:
        """
        The objective is to generate solutions to a given programming puzzle problem.
        Args:
            seed: the seed dict.
            config: the config file path or dict.
            mutation_model: the diff model (or alternatives).
        """
        self.mutation_model = mutation_model
        self.config = config
        self.batch_size = self.config.batch_size
        self.seed_index = self.config.starting_seed

        # Get info for the puzzle that will be solved
        # This puzzle is at the index of the puzzles array specified by self.seed_index
        puzzles = requests.get("https://raw.githubusercontent.com/microsoft/PythonProgrammingPuzzles/v0.2/puzzles/puzzles.json").json()
        puzzle = puzzles[self.seed_index]

        self.problem_func = puzzle['sat'].replace('def sat(', 'def f6(') # prompt form is f6()
        self.solution_preamble = puzzle['sol_header'].replace('def sol(', 'def g6(') # solution form is g6()
        if self.config.prompt_size == 'long':
            self.solution_preamble += '\n' + puzzle['sol_docstring'] # add in the docstring
        self.ans_type = puzzle['ans_type']
        
        if self.config.prompt_size == 'long':
            self.prompt_seed = P3_PROBLEM_LONG_SEED
        elif self.config.prompt_size == 'med':
            self.prompt_seed = P3_PROBLEM_MED_SEED
        else:
            raise ValueError('No seed string found')

        # dummy to get shape
        dummy_pl = pipeline('feature-extraction', model=self.mutation_model.config.model_path)
        dummy_scaler = StandardScaler()
        dummy_features = np.array(dummy_pl(self.prompt_seed))
        dummy_features_scaled = dummy_scaler.fit_transform(np.squeeze(dummy_features))
        dummy_pca = PCA(.95)
        dummy_pca_features = dummy_pca.fit_transform(np.squeeze(dummy_features_scaled))
        self.genotype_ndim: int = dummy_pca_features.shape[-1]
        self.genotype_space = np.repeat([[-20, 20]], self.genotype_ndim, axis=0).T
        self.rng = None

    def get_rng_state(self) -> Optional[np.random._generator.Generator]:
        warnings.warn("WARNING: rng state not used in this environment")
        return None

    def set_rng_state(self, rng_state: Optional[np.random._generator.Generator]):
        warnings.warn("WARNING: rng state not used in this environment")
        pass
    
    def construct_prompt(self, code_batch: Optional[Union[list[str], str]] = None) -> dict[str, str]:
        prompt_str = self.prompt_seed

        prompt_str += (
            f'\n\n{self.problem_func}' # add this particular problem, f6(), to the prompt
            f'\n\n# Old version of g6()' 
            f'\n# TODO: fix bugs in the code below\n' 
        )
        if code_batch is None:
            prompt_str += ""
        else:
            if isinstance(code_batch, list):
                # TODO: get nearby genotypes
                prompt_str += code_batch[0]
            elif isinstance(code_batch, str):
                prompt_str += code_batch

        prompt_str += (
            f'\n\n# Fixed version of g6()' 
            f'\n{self.solution_preamble}'
        )

        template = f'{P3_IMPORTS}\n{self.solution_preamble}'
        return {'prompt': prompt_str, 'template': template}

    def generate_programs(self, code_batch: list[str]) -> list[P3Solution]:
        """Generate new programs with a mutation model and evaluate them."""
        local_scope_exec = True
        generated_programs = self.mutation_model.generate_programs(
            code_batch, local_scope_exec
        )

        if self.config.sandbox:
            results = []
            for code in generated_programs:
                resp = requests.post(
                    f"{self.sandbox_server}/eval_p3_solution",
                    json={"code": code, "timeout": self.config.timeout},
                    timeout=self.config.timeout,
                )
                if resp.status_code == 200:
                    return_dict = json.loads(resp.text)
                    results.append(return_dict)
        else:
            results = pool_exec_processes(
                generated_programs,
                func_name="g6",
                timeout=self.config.timeout,
                processes=self.config.processes,
                debug=self.config.debug,
            )
        results = [{'program_str': gen_prog, 'result_obj': res_obj, 'model': self.mutation_model.config.model_path}
                    for (gen_prog, res_obj) in zip(generated_programs, results)]
        return [P3Solution(**p) for p in results]

    def fitness(self, sol: P3Solution) -> float:
        """
        If passing the solution to the problem returns True, fitness is 1.0
            else -np.inf
        """
        # TODO: consider how to have a larger spectrum than binary fitness

        if not type_check(self.ans_type, sol.result_obj): return -np.inf

        eval_code = ( 
            f"{P3_IMPORTS}\n"
            f"{self.problem_func}\n"
            f"def run_eval():\n"
            f"    return f6({sol.result_obj})"
        )

        result = pool_exec_processes(
            eval_code,
            func_name='run_eval',
            timeout=self.config.timeout,
            processes=self.config.processes,
            debug=self.config.debug,
        )
        if result[0] == True:
            return 1.0
        else:
            return -np.inf

    def random(self) -> list[P3Solution]:
        program_list = [self.construct_prompt() for _ in range(self.config.batch_size)]
        new_solutions = self.generate_programs(program_list)
        return new_solutions

    def mutate(self, sol_list: list[P3Solution]) -> list[P3Solution]:
        sols = [s.program_str for s in sol_list]
        program_list = list(map(self.construct_prompt, sols))
        new_sols = self.generate_programs(program_list)
        return new_sols 

class P3ProbSolResult(Genotype):
    def __init__(self, program_str: str, result_obj: dict, model: str):
        """
        Genotype for a programming puzzle problem+solution pair.
        Args:
            program_str: the code for the pair.
            result_obj: the result of the solution.
            model: the model whose embeddings will create the phenotype
        """
        self.program_str = program_str
        self.result_obj = result_obj
        self.model = model

        self.pl = pipeline('feature-extraction', model=self.model)
        seed_features = np.array(self.pl(P3_PROBSOL_LONG_SEED))
        
        self.scaler = StandardScaler()
        seed_features_scaled = self.scaler.fit_transform(np.squeeze(seed_features))
        self.pca = PCA(.95)
        self.pca.fit(seed_features_scaled)

    def __str__(self) -> str:
        return self.program_str

    def to_phenotype(self) -> Optional[Phenotype]:
        features = np.array(self.pl(self.program_str))
        features_scaled = self.scaler.transform(np.squeeze(features))
        pca_features = self.pca.transform(features_scaled)
        return pca_features.max(axis=0).flatten()

class P3ProbSol(BaseEnvironment[P3ProbSolResult]):

    def __init__(
        self,
        config: P3ProbSolEnvConfig,
        mutation_model: MutationModel,
    ) -> None:
        """
        The objective is to generate problem+solution pairs.
        Args:
            config: the config file path or dict.
            mutation_model: the diff model (or alternatives).
            ans_type: answer type
        """
        self.mutation_model = mutation_model
        self.config = config
        self.batch_size = self.config.batch_size
        self.seed_index = self.config.starting_seed

        if self.config.prompt_size == 'long':
            self.prompt_seed = P3_PROBSOL_LONG_SEED
        else:
            raise ValueError('No seed string found')

        # dummy to get shape
        dummy_pl = pipeline('feature-extraction', model=self.mutation_model.config.model_path)
        dummy_scaler = StandardScaler()
        dummy_features = np.array(dummy_pl(self.prompt_seed))
        dummy_features_scaled = dummy_scaler.fit_transform(np.squeeze(dummy_features))
        dummy_pca = PCA(.95)
        dummy_pca_features = dummy_pca.fit_transform(np.squeeze(dummy_features_scaled))
        self.genotype_ndim: int = dummy_pca_features.shape[-1]
        self.genotype_space = np.repeat([[-20, 20]], self.genotype_ndim, axis=0).T
        self.rng = None


        # Get info for the seed puzzle that will be mutated
        # This puzzle is at the index of the puzzles array specified by self.seed_index
        # TODO: put this in a method/construct_prompt?
        puzzles = requests.get("https://raw.githubusercontent.com/microsoft/PythonProgrammingPuzzles/v0.2/puzzles/puzzles.json").json()
        puzzle = puzzles[self.seed_index]
        if len(puzzle['sol_bodies']) == 0:
            raise ValueError(f'No sample solution is provided for the puzzle at index {self.seed_index}')

        f6_1 = puzzle['sat'].replace('def sat(', 'def f6_1(') # problem form is f6_1()
        g6_1 = puzzle['sol_header'].replace('def sol(', 'def g6_1(') # solution form is g6_1()
        if self.config.prompt_size == 'long':
            g6_1 += '\n' + puzzle['sol_docstring'] # add in the docstring
        g6_1 += '\n' + puzzle['sol_bodies'][0] # include the first example solution function body

        self.original_probsol = f6_1 + '\n\n' + g6_1 + '\n\n' + "assert f6_1(g6_1())" 
        self.new_probsol_preamble = 'def f6_2(' 

    def get_rng_state(self) -> Optional[np.random._generator.Generator]:
        warnings.warn("WARNING: rng state not used in this environment")
        return None
    
    def set_rng_state(self, rng_state: Optional[np.random._generator.Generator]):
        warnings.warn("WARNING: rng state not used in this environment")
        pass

    def construct_prompt(self, code_batch: Optional[Union[list[str], str]] = None) -> dict[str, str]:
        prompt_str = self.prompt_seed

        if code_batch is None:
            # prompt with prob+sol from P3 dataset 
            prompt_str += (
                f'\n\n{self.original_probsol}' # add this particular probsol, f6_1() and g6_1(), to the prompt
                f'\n\n{self.new_probsol_preamble}' # add f6_2() preamble to the prompt
            )
        else:
            # prompt with prob+sol that is given (one that was the output of a prev mutation)
            if isinstance(code_batch, list):
                # TODO: get nearby genotypes
                program_str = code_batch[0]
            elif isinstance(code_batch, str):
                program_str = code_batch

            # the prev output was f6_2 and g6_2, so now make it f6_1 and g6_1 for the prompt
            # and remove comments from f6_1
            # TODO: consider if there is a better structure than all this string parsing
            program_str = program_str.replace('def f6_2(', 'def f6_1(')
            program_str = program_str.replace('def g6_2(', 'def g6_1(')
            i = program_str.find('def g6_1(')
            program_str = re.sub('""".*"""', '', program_str[:i]) + program_str[i:]
            prompt_str += (
                f'\n\n{program_str}'
                f'\n\n{self.new_probsol_preamble}'
            )

        template = f'{P3_IMPORTS}\n{self.new_probsol_preamble}'
        return {'prompt': prompt_str, 'template': template}

    def generate_programs(self, code_batch: list[str]) -> list[P3ProbSolResult]:
        """Generate new programs with a mutation model and evaluate them."""
        local_scope_exec = False
        generated_programs = self.mutation_model.generate_programs(
            code_batch, local_scope_exec
        )

        # Remove assert statement (last line) from the generated program
        # since during evaluation it causes an exception if the solution
        # is incorrect, but we want to return that it was an incorrect
        # solution as opposed to an un-runnable program
        for i, gp in enumerate(generated_programs):
            lines = gp.strip().split('\n')[:-1]
            gp = '\n'.join(lines)
            generated_programs[i] = gp

        if self.config.sandbox:
            results = []
            for code in generated_programs:
                resp = requests.post(
                    f"{self.sandbox_server}/eval_p3_solution",
                    json={"code": code, "timeout": self.config.timeout},
                    timeout=self.config.timeout,
                )
                if resp.status_code == 200:
                    return_dict = json.loads(resp.text)
                    results.append(return_dict)
        else:
            results = pool_exec_processes(
                generated_programs,
                func_name="g6_2",
                timeout=self.config.timeout,
                processes=self.config.processes,
                debug=self.config.debug,
            )

        results = [{'program_str': gen_prog, 'result_obj': res_obj, 'model': self.mutation_model.config.model_path}
                    for (gen_prog, res_obj) in zip(generated_programs, results)]
        return [P3ProbSolResult(**p) for p in results]

    def fitness(self, probsol: P3ProbSolResult) -> float:
        """
        If passing the solution to the problem returns True, fitness is 1.0
            else -np.inf
        """
        if isinstance(probsol.result_obj, ExecResult):
            return -np.inf
        
        # TODO: check type expected by f6_2 if any?
        # TODO: check for triviality of f6_2 requirements s.t. trivial == bad fitness?

        eval_code = ( 
            f"{P3_IMPORTS}\n"
            f"{probsol.program_str}\n"
            f"def run_eval():\n"
            f"    return f6_2({probsol.result_obj})"
        )

        result = pool_exec_processes(
            eval_code,
            func_name='run_eval',
            timeout=self.config.timeout,
            processes=self.config.processes,
            debug=self.config.debug,
        )
        # TODO: consider how to have a larger spectrum than binary fitness
        if result[0] == True:
            return 1.0
        else:
            return -np.inf

    def random(self) -> list[P3ProbSolResult]:
        program_list = [self.construct_prompt() for _ in range(self.config.batch_size)]
        new_probsols = self.generate_programs(program_list)
        return new_probsols

    def mutate(self, probsol_list: list[P3ProbSolResult]) -> list[P3ProbSolResult]:
        probsols = [pb.program_str for pb in probsol_list]
        program_list = list(map(self.construct_prompt, probsols))
        new_probsols = self.generate_programs(program_list)
        return new_probsols 

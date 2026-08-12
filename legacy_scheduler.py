"""Faithful Python port of the legacy Java scheduler backend.

The public dataclasses mirror the original scheduler package.  Genetic genes
are permutations of each student group's weekly slots, fitness is based on
cross-group teacher collisions, and the engine supports elitism, roulette
selection, group crossover, custom mutation, rotation mutation and swap
mutation.  No web/UI or database concerns live in this module.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import random
from typing import Iterable


@dataclass
class Teacher:
    id: int
    name: str
    subject: str
    assigned: int = 0


@dataclass
class Subject:
    id: int
    name: str
    teachers: list[Teacher] = field(default_factory=list)


@dataclass
class StudentGroup:
    id: int
    name: str
    subjects: list[str]
    hours: list[int]
    teacher_ids: list[int] = field(default_factory=list)

    @property
    def subject_count(self) -> int:
        return len(self.subjects)


@dataclass
class Slot:
    student_group_id: int
    teacher_id: int
    subject: str


@dataclass
class InputData:
    student_groups: list[StudentGroup]
    teachers: list[Teacher]
    hours_per_day: int = 7
    days_per_week: int = 5
    day_names: list[str] = field(default_factory=list)
    period_times: list[dict] = field(default_factory=list)
    break_slot: int | None = None
    break_start: str | None = None
    break_end: str | None = None
    crossover_rate: float = 1.0
    mutation_rate: float = 0.1

    def __post_init__(self):
        if not self.day_names:
            names=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
            self.day_names=names[:self.days_per_week]
        if not self.period_times:
            self.period_times=[{'slot':i+1,'start':'','end':''} for i in range(self.hours_per_day)]
        if len(self.day_names)!=self.days_per_week:
            raise ValueError('day_names length must equal days_per_week')
        if len(self.period_times)!=self.hours_per_day:
            raise ValueError('period_times length must equal hours_per_day')
        if not self.student_groups: raise ValueError('At least one student group is required')
        self.assign_teachers()
        capacity=self.hours_per_day*self.days_per_week
        for group in self.student_groups:
            if sum(group.hours)>capacity:
                raise ValueError(f'{group.name} requires {sum(group.hours)} periods but only {capacity} are available')

    def assign_teachers(self):
        """Original least-assigned eligible teacher algorithm."""
        for teacher in self.teachers: teacher.assigned=0
        for group in self.student_groups:
            group.teacher_ids=[]
            for subject in group.subjects:
                eligible=[t for t in self.teachers if t.subject.casefold()==subject.casefold()]
                if not eligible: raise ValueError(f'No teacher teaches {subject} (group {group.name})')
                selected=min(eligible,key=lambda t:(t.assigned,t.id))
                selected.assigned+=1;group.teacher_ids.append(selected.id)


def parse_legacy_text(text: str, **settings) -> InputData:
    """Parse the source application's studentgroups/teachers/end format."""
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    lowered=[line.casefold() for line in lines]
    if 'studentgroups' not in lowered or 'teachers' not in lowered or 'end' not in lowered:
        raise ValueError('Input must contain studentgroups, teachers and end sections')
    sg_start=lowered.index('studentgroups')+1; teacher_start=lowered.index('teachers'); end=lowered.index('end',teacher_start)
    groups=[]
    for gid,line in enumerate(lines[sg_start:teacher_start]):
        tokens=line.split()
        if len(tokens)<3 or len(tokens[1:])%2: raise ValueError(f'Invalid student group line: {line}')
        subjects=tokens[1::2]
        try: hours=[int(v) for v in tokens[2::2]]
        except ValueError as exc: raise ValueError(f'Invalid hours in: {line}') from exc
        groups.append(StudentGroup(gid,tokens[0],subjects,hours))
    teachers=[]
    for tid,line in enumerate(lines[teacher_start+1:end]):
        tokens=line.split()
        if len(tokens)!=2: raise ValueError(f'Invalid teacher line: {line}')
        teachers.append(Teacher(tid,tokens[0],tokens[1]))
    return InputData(groups,teachers,**settings)


def class_format(line: str) -> bool:
    """Port of inputdata.classformat: a class line has exactly three tokens."""
    return len(line.split()) == 3


class TimeTable:
    def __init__(self,data:InputData):
        self.data=data;self.slots=[];capacity=data.hours_per_day*data.days_per_week
        for group in data.student_groups:
            group_slots=[]
            for subject,hours,teacher_id in zip(group.subjects,group.hours,group.teacher_ids):
                group_slots.extend([Slot(group.id,teacher_id,subject) for _ in range(hours)])
            group_slots.extend([None]*(capacity-len(group_slots)))
            self.slots.extend(group_slots)


@dataclass
class Gene:
    slot_numbers:list[int]
    @classmethod
    def random(cls,group_id:int,capacity:int,rng:random.Random):
        values=list(range(group_id*capacity,(group_id+1)*capacity));rng.shuffle(values);return cls(values)


@dataclass
class Chromosome:
    genes:list[Gene]
    fitness:float=0.0
    collisions:int=0

    def calculate_fitness(self,timetable:TimeTable)->float:
        data=timetable.data;capacity=data.hours_per_day*data.days_per_week;collisions=0
        for position in range(capacity):
            teachers=[]
            for gene in self.genes:
                slot=timetable.slots[gene.slot_numbers[position]]
                if slot is not None:
                    if slot.teacher_id in teachers:collisions+=1
                    else:teachers.append(slot.teacher_id)
        denominator=(len(data.student_groups)-1.0)*capacity
        self.collisions=collisions
        self.fitness=1.0 if denominator<=0 else 1-(collisions/denominator)
        return self.fitness

    def raw(self) -> list[list[int]]:
        """Port of printChromosome, returned as structured data."""
        return [list(gene.slot_numbers) for gene in self.genes]


class SchedulerMain:
    def __init__(self,data:InputData,population_size:int=1000,max_generations:int=100,seed:int|None=None,mutation_attempts:int=5000):
        self.data=data;self.timetable=TimeTable(data);self.population_size=max(10,population_size);self.max_generations=max(1,max_generations);self.rng=random.Random(seed);self.mutation_attempts=mutation_attempts;self.history=[]

    @property
    def capacity(self):return self.data.hours_per_day*self.data.days_per_week
    def chromosome(self):
        c=Chromosome([Gene.random(i,self.capacity,self.rng) for i in range(len(self.data.student_groups))]);c.calculate_fitness(self.timetable);return c
    def select_parent_roulette(self,elite):
        total=sum(c.fitness for c in elite);point=self.rng.random()*total;current=0
        for chromosome in elite:
            current+=chromosome.fitness
            if current>=point:return deepcopy(chromosome)
        return deepcopy(elite[-1])
    def select_parent_best(self,population):return deepcopy(self.rng.choice(population[:min(100,len(population))]))
    def crossover(self,father,mother):
        index=self.rng.randrange(len(father.genes));father.genes[index],mother.genes[index]=deepcopy(mother.genes[index]),deepcopy(father.genes[index]);father.calculate_fitness(self.timetable);mother.calculate_fitness(self.timetable);return father if father.fitness>mother.fitness else mother
    def custom_mutation(self,chromosome):
        old=chromosome.fitness;group=self.rng.randrange(len(chromosome.genes));best=deepcopy(chromosome)
        for _ in range(self.mutation_attempts):
            candidate=deepcopy(chromosome);candidate.genes[group]=Gene.random(group,self.capacity,self.rng);candidate.calculate_fitness(self.timetable)
            if candidate.fitness>=best.fitness:best=candidate
            if best.fitness>=old:break
        return best
    def mutation(self,chromosome):
        group=self.rng.randrange(len(chromosome.genes));values=chromosome.genes[group].slot_numbers;chromosome.genes[group].slot_numbers=values[1:]+values[:1];chromosome.calculate_fitness(self.timetable);return chromosome
    def swap_mutation(self,chromosome):
        group=self.rng.randrange(len(chromosome.genes));i,j=self.rng.sample(range(self.capacity),2);values=chromosome.genes[group].slot_numbers;values[i],values[j]=values[j],values[i];chromosome.calculate_fitness(self.timetable);return chromosome
    def run(self):
        population=sorted([self.chromosome() for _ in range(self.population_size)],key=lambda c:c.fitness,reverse=True);best=deepcopy(population[0])
        for generation in range(self.max_generations):
            checkpoints=sorted(set([0,min(1,len(population)-1),min(2,len(population)-1),min(3,len(population)-1),min(self.population_size//10+1,len(population)-1),min(self.population_size//5+1,len(population)-1)]))
            self.history.append({'generation':generation+1,'best_fitness':population[0].fitness,'collisions':population[0].collisions,'checkpoints':[{'chromosome':i,'fitness':population[i].fitness,'collisions':population[i].collisions,'genes':population[i].raw()} for i in checkpoints]})
            if population[0].fitness>=1:best=deepcopy(population[0]);break
            elite=population[:max(1,self.population_size//10)];new=[deepcopy(c) for c in elite]
            while len(new)<self.population_size:
                father=self.select_parent_roulette(elite);mother=self.select_parent_roulette(elite)
                child=self.crossover(father,mother) if self.rng.random()<self.data.crossover_rate else father
                # SchedulerMain invokes customMutation for every child.  The
                # legacy mutation-rate field is retained as configuration data,
                # matching the Java implementation where it was not consulted.
                child=self.custom_mutation(child)
                new.append(child)
                if child.fitness>best.fitness:best=deepcopy(child)
            population=sorted(new,key=lambda c:c.fitness,reverse=True)
            if population[0].fitness>best.fitness:best=deepcopy(population[0])
        return best
    def render(self,chromosome):
        output=[]
        for group,gene in zip(self.data.student_groups,chromosome.genes):
            days=[]
            for day_index,day_name in enumerate(self.data.day_names):
                periods=[]
                for period_index in range(self.data.hours_per_day):
                    position=day_index*self.data.hours_per_day+period_index;slot=self.timetable.slots[gene.slot_numbers[position]];timing=self.data.period_times[period_index]
                    periods.append({'slot_number':period_index+1,'start_time':timing.get('start',''),'end_time':timing.get('end',''),'is_break':self.data.break_slot==period_index+1,'subject':slot.subject if slot else None,'teacher_id':slot.teacher_id if slot else None,'teacher_name':self.data.teachers[slot.teacher_id].name if slot else None,'free':slot is None})
                days.append({'day':day_name,'periods':periods})
            output.append({'group_id':group.id,'group_name':group.name,'days':days})
        return output


def input_summary(data:InputData):
    return {'student_group_count':len(data.student_groups),'teacher_count':len(data.teachers),'days_per_week':data.days_per_week,'hours_per_day':data.hours_per_day,'groups':[{'id':g.id,'name':g.name,'subjects':[{'name':s,'hours':h,'teacher_id':t} for s,h,t in zip(g.subjects,g.hours,g.teacher_ids)]} for g in data.student_groups],'teachers':[vars(t) for t in data.teachers]}


def slot_summary(timetable:TimeTable):
    return [{'slot_index':i,'free':slot is None,**({} if slot is None else {'student_group_id':slot.student_group_id,'teacher_id':slot.teacher_id,'subject':slot.subject})} for i,slot in enumerate(timetable.slots)]

